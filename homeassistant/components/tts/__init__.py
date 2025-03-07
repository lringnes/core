"""Provide functionality for TTS."""
from __future__ import annotations

from abc import abstractmethod
import asyncio
from collections.abc import Mapping
from datetime import datetime
from functools import partial
import hashlib
from http import HTTPStatus
import io
import logging
import mimetypes
import os
import re
from typing import Any, TypedDict, final

from aiohttp import web
import mutagen
from mutagen.id3 import ID3, TextFrame as ID3Text
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import (
    ATTR_MEDIA_ANNOUNCE,
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as DOMAIN_MP,
    SERVICE_PLAY_MEDIA,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    PLATFORM_FORMAT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HassJob, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import get_url
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util, language as language_util

from .const import (
    ATTR_CACHE,
    ATTR_LANGUAGE,
    ATTR_MESSAGE,
    ATTR_OPTIONS,
    CONF_BASE_URL,
    CONF_CACHE,
    CONF_CACHE_DIR,
    CONF_TIME_MEMORY,
    DATA_TTS_MANAGER,
    DEFAULT_CACHE,
    DEFAULT_CACHE_DIR,
    DEFAULT_TIME_MEMORY,
    DOMAIN,
    TtsAudioType,
)
from .helper import get_engine_instance
from .legacy import PLATFORM_SCHEMA, PLATFORM_SCHEMA_BASE, Provider, async_setup_legacy
from .media_source import generate_media_source_id, media_source_id_to_kwargs
from .models import Voice

__all__ = [
    "async_default_engine",
    "async_get_media_source_audio",
    "async_support_options",
    "ATTR_AUDIO_OUTPUT",
    "CONF_LANG",
    "DEFAULT_CACHE_DIR",
    "generate_media_source_id",
    "get_base_url",
    "PLATFORM_SCHEMA_BASE",
    "PLATFORM_SCHEMA",
    "Provider",
    "TtsAudioType",
    "Voice",
]

_LOGGER = logging.getLogger(__name__)

ATTR_PLATFORM = "platform"
ATTR_AUDIO_OUTPUT = "audio_output"
ATTR_MEDIA_PLAYER_ENTITY_ID = "media_player_entity_id"
ATTR_VOICE = "voice"

CONF_LANG = "language"

BASE_URL_KEY = "tts_base_url"

SERVICE_CLEAR_CACHE = "clear_cache"

_RE_LEGACY_VOICE_FILE = re.compile(
    r"([a-f0-9]{40})_([^_]+)_([^_]+)_([a-z_]+)\.[a-z0-9]{3,4}"
)
_RE_VOICE_FILE = re.compile(
    r"([a-f0-9]{40})_([^_]+)_([^_]+)_(tts\.[a-z0-9_]+)\.[a-z0-9]{3,4}"
)
KEY_PATTERN = "{0}_{1}_{2}_{3}"

SCHEMA_SERVICE_CLEAR_CACHE = vol.Schema({})


class TTSCache(TypedDict):
    """Cached TTS file."""

    filename: str
    voice: bytes
    pending: asyncio.Task | None


@callback
def async_default_engine(hass: HomeAssistant) -> str | None:
    """Return the domain or entity id of the default engine.

    Returns None if no engines found.
    """
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    if "cloud" in manager.providers:
        return "cloud"

    entity = next(iter(component.entities), None)

    if entity is not None:
        return entity.entity_id

    return next(iter(manager.providers), None)


@callback
def async_resolve_engine(hass: HomeAssistant, engine: str | None) -> str | None:
    """Resolve engine.

    Returns None if no engines found or invalid engine passed in.
    """
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    if engine is not None:
        if not component.get_entity(engine) and engine not in manager.providers:
            return None
        return engine

    return async_default_engine(hass)


async def async_support_options(
    hass: HomeAssistant,
    engine: str,
    language: str | None = None,
    options: dict | None = None,
) -> bool:
    """Return if an engine supports options."""
    if (engine_instance := get_engine_instance(hass, engine)) is None:
        raise HomeAssistantError(f"Provider {engine} not found")

    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    try:
        manager.process_options(engine_instance, language, options)
    except HomeAssistantError:
        return False

    return True


async def async_get_media_source_audio(
    hass: HomeAssistant,
    media_source_id: str,
) -> tuple[str, bytes]:
    """Get TTS audio as extension, data."""
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]
    return await manager.async_get_tts_audio(
        **media_source_id_to_kwargs(media_source_id),
    )


@callback
def async_get_text_to_speech_languages(hass: HomeAssistant) -> set[str]:
    """Return a set with the union of languages supported by tts engines."""
    languages = set()

    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    for entity in component.entities:
        for language_tag in entity.supported_languages:
            languages.add(language_tag)

    for tts_engine in manager.providers.values():
        for language_tag in tts_engine.supported_languages:
            languages.add(language_tag)

    return languages


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up TTS."""
    websocket_api.async_register_command(hass, websocket_list_engines)
    websocket_api.async_register_command(hass, websocket_get_engine)
    websocket_api.async_register_command(hass, websocket_list_engine_voices)

    # Legacy config options
    conf = config[DOMAIN][0] if config.get(DOMAIN) else {}
    use_cache: bool = conf.get(CONF_CACHE, DEFAULT_CACHE)
    cache_dir: str = conf.get(CONF_CACHE_DIR, DEFAULT_CACHE_DIR)
    time_memory: int = conf.get(CONF_TIME_MEMORY, DEFAULT_TIME_MEMORY)
    base_url: str | None = conf.get(CONF_BASE_URL)
    if base_url is not None:
        _LOGGER.warning(
            "TTS base_url option is deprecated. Configure internal/external URL"
            " instead"
        )
    hass.data[BASE_URL_KEY] = base_url

    tts = SpeechManager(hass, use_cache, cache_dir, time_memory, base_url)

    try:
        await tts.async_init_cache()
    except (HomeAssistantError, KeyError):
        _LOGGER.exception("Error on cache init")
        return False

    hass.data[DATA_TTS_MANAGER] = tts
    component = hass.data[DOMAIN] = EntityComponent[TextToSpeechEntity](
        _LOGGER, DOMAIN, hass
    )

    component.register_shutdown()

    hass.http.register_view(TextToSpeechView(tts))
    hass.http.register_view(TextToSpeechUrlView(tts))

    platform_setups = await async_setup_legacy(hass, config)

    if platform_setups:
        await asyncio.wait([asyncio.create_task(setup) for setup in platform_setups])

    component.async_register_entity_service(
        "speak",
        {
            vol.Required(ATTR_MEDIA_PLAYER_ENTITY_ID): cv.comp_entity_ids,
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_CACHE, default=DEFAULT_CACHE): cv.boolean,
            vol.Optional(ATTR_LANGUAGE): cv.string,
            vol.Optional(ATTR_OPTIONS): dict,
        },
        "async_speak",
    )

    async def async_clear_cache_handle(service: ServiceCall) -> None:
        """Handle clear cache service call."""
        await tts.async_clear_cache()

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CACHE,
        async_clear_cache_handle,
        schema=SCHEMA_SERVICE_CLEAR_CACHE,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    return await component.async_setup_entry(entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    return await component.async_unload_entry(entry)


class TextToSpeechEntity(RestoreEntity):
    """Represent a single TTS engine."""

    _attr_should_poll = False
    __last_tts_loaded: str | None = None

    @property
    @final
    def state(self) -> str | None:
        """Return the state of the entity."""
        if self.__last_tts_loaded is None:
            return None
        return self.__last_tts_loaded

    @property
    @abstractmethod
    def supported_languages(self) -> list[str]:
        """Return a list of supported languages."""

    @property
    @abstractmethod
    def default_language(self) -> str:
        """Return the default language."""

    @property
    def supported_options(self) -> list[str] | None:
        """Return a list of supported options like voice, emotions."""
        return None

    @property
    def default_options(self) -> Mapping[str, Any] | None:
        """Return a mapping with the default options."""
        return None

    @callback
    def async_get_supported_voices(self, language: str) -> list[Voice] | None:
        """Return a list of supported voices for a language."""
        return None

    async def async_internal_added_to_hass(self) -> None:
        """Call when the entity is added to hass."""
        await super().async_internal_added_to_hass()
        state = await self.async_get_last_state()
        if (
            state is not None
            and state.state is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            self.__last_tts_loaded = state.state

    async def async_speak(
        self,
        media_player_entity_id: list[str],
        message: str,
        cache: bool,
        language: str | None = None,
        options: dict | None = None,
    ) -> None:
        """Speak via a Media Player."""
        await self.hass.services.async_call(
            DOMAIN_MP,
            SERVICE_PLAY_MEDIA,
            {
                ATTR_ENTITY_ID: media_player_entity_id,
                ATTR_MEDIA_CONTENT_ID: generate_media_source_id(
                    self.hass,
                    message=message,
                    engine=self.entity_id,
                    language=language,
                    options=options,
                    cache=cache,
                ),
                ATTR_MEDIA_CONTENT_TYPE: MediaType.MUSIC,
                ATTR_MEDIA_ANNOUNCE: True,
            },
            blocking=True,
            context=self._context,
        )

    @final
    async def internal_async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> TtsAudioType:
        """Process an audio stream to TTS service.

        Only streaming content is allowed!
        """
        self.__last_tts_loaded = dt_util.utcnow().isoformat()
        self.async_write_ha_state()
        return await self.async_get_tts_audio(
            message=message, language=language, options=options
        )

    def get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> TtsAudioType:
        """Load tts audio file from the engine."""
        raise NotImplementedError()

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict[str, Any] | None = None
    ) -> TtsAudioType:
        """Load tts audio file from the engine.

        Return a tuple of file extension and data as bytes.
        """
        return await self.hass.async_add_executor_job(
            partial(self.get_tts_audio, message, language, options=options)
        )


def _hash_options(options: dict) -> str:
    """Hashes an options dictionary."""
    opts_hash = hashlib.blake2s(digest_size=5)
    for key, value in sorted(options.items()):
        opts_hash.update(str(key).encode())
        opts_hash.update(str(value).encode())

    return opts_hash.hexdigest()


class SpeechManager:
    """Representation of a speech store."""

    def __init__(
        self,
        hass: HomeAssistant,
        use_cache: bool,
        cache_dir: str,
        time_memory: int,
        base_url: str | None,
    ) -> None:
        """Initialize a speech store."""
        self.hass = hass
        self.providers: dict[str, Provider] = {}

        self.use_cache = use_cache
        self.cache_dir = cache_dir
        self.time_memory = time_memory
        self.base_url = base_url
        self.file_cache: dict[str, str] = {}
        self.mem_cache: dict[str, TTSCache] = {}

    async def async_init_cache(self) -> None:
        """Init config folder and load file cache."""
        try:
            self.cache_dir = await self.hass.async_add_executor_job(
                _init_tts_cache_dir, self.hass, self.cache_dir
            )
        except OSError as err:
            raise HomeAssistantError(f"Can't init cache dir {err}") from err

        try:
            cache_files = await self.hass.async_add_executor_job(
                _get_cache_files, self.cache_dir
            )
        except OSError as err:
            raise HomeAssistantError(f"Can't read cache dir {err}") from err

        if cache_files:
            self.file_cache.update(cache_files)

    async def async_clear_cache(self) -> None:
        """Read file cache and delete files."""
        self.mem_cache = {}

        def remove_files() -> None:
            """Remove files from filesystem."""
            for filename in self.file_cache.values():
                try:
                    os.remove(os.path.join(self.cache_dir, filename))
                except OSError as err:
                    _LOGGER.warning("Can't remove cache file '%s': %s", filename, err)

        await self.hass.async_add_executor_job(remove_files)
        self.file_cache = {}

    @callback
    def async_register_legacy_engine(
        self, engine: str, provider: Provider, config: ConfigType
    ) -> None:
        """Register a legacy TTS engine."""
        provider.hass = self.hass
        if provider.name is None:
            provider.name = engine
        self.providers[engine] = provider

        self.hass.config.components.add(
            PLATFORM_FORMAT.format(domain=engine, platform=DOMAIN)
        )

    @callback
    def process_options(
        self,
        engine_instance: TextToSpeechEntity | Provider,
        language: str | None = None,
        options: dict | None = None,
    ) -> tuple[str, dict | None]:
        """Validate and process options."""
        # Languages
        language = language or engine_instance.default_language
        if (
            language is None
            or engine_instance.supported_languages is None
            or language not in engine_instance.supported_languages
        ):
            raise HomeAssistantError(f"Language '{language}' not supported")

        # Options
        if (default_options := engine_instance.default_options) and options:
            merged_options = dict(default_options)
            merged_options.update(options)
            options = merged_options
        if not options:
            options = None if default_options is None else dict(default_options)

        if options is not None:
            supported_options = engine_instance.supported_options or []
            invalid_opts = [
                opt_name for opt_name in options if opt_name not in supported_options
            ]
            if invalid_opts:
                raise HomeAssistantError(f"Invalid options found: {invalid_opts}")

        return language, options

    async def async_get_url_path(
        self,
        engine: str,
        message: str,
        cache: bool | None = None,
        language: str | None = None,
        options: dict | None = None,
    ) -> str:
        """Get URL for play message.

        This method is a coroutine.
        """
        if (engine_instance := get_engine_instance(self.hass, engine)) is None:
            raise HomeAssistantError(f"Provider {engine} not found")

        language, options = self.process_options(engine_instance, language, options)
        cache_key = self._generate_cache_key(message, language, options, engine)
        use_cache = cache if cache is not None else self.use_cache

        # Is speech already in memory
        if cache_key in self.mem_cache:
            filename = self.mem_cache[cache_key]["filename"]
        # Is file store in file cache
        elif use_cache and cache_key in self.file_cache:
            filename = self.file_cache[cache_key]
            self.hass.async_create_task(self._async_file_to_mem(cache_key))
        # Load speech from engine into memory
        else:
            filename = await self._async_get_tts_audio(
                engine_instance,
                cache_key,
                message,
                use_cache,
                language,
                options,
            )

        return f"/api/tts_proxy/{filename}"

    async def async_get_tts_audio(
        self,
        engine: str,
        message: str,
        cache: bool | None = None,
        language: str | None = None,
        options: dict | None = None,
    ) -> tuple[str, bytes]:
        """Fetch TTS audio."""
        if (engine_instance := get_engine_instance(self.hass, engine)) is None:
            raise HomeAssistantError(f"Provider {engine} not found")

        language, options = self.process_options(engine_instance, language, options)
        cache_key = self._generate_cache_key(message, language, options, engine)
        use_cache = cache if cache is not None else self.use_cache

        # If we have the file, load it into memory if necessary
        if cache_key not in self.mem_cache:
            if use_cache and cache_key in self.file_cache:
                await self._async_file_to_mem(cache_key)
            else:
                await self._async_get_tts_audio(
                    engine_instance, cache_key, message, use_cache, language, options
                )

        extension = os.path.splitext(self.mem_cache[cache_key]["filename"])[1][1:]
        cached = self.mem_cache[cache_key]
        if pending := cached.get("pending"):
            await pending
            cached = self.mem_cache[cache_key]
        return extension, cached["voice"]

    @callback
    def _generate_cache_key(
        self,
        message: str,
        language: str,
        options: dict | None,
        engine: str,
    ) -> str:
        """Generate a cache key for a message."""
        options_key = _hash_options(options) if options else "-"
        msg_hash = hashlib.sha1(bytes(message, "utf-8")).hexdigest()
        return KEY_PATTERN.format(
            msg_hash, language.replace("_", "-"), options_key, engine
        ).lower()

    async def _async_get_tts_audio(
        self,
        engine_instance: TextToSpeechEntity | Provider,
        cache_key: str,
        message: str,
        cache: bool,
        language: str,
        options: dict | None,
    ) -> str:
        """Receive TTS, store for view in cache and return filename.

        This method is a coroutine.
        """
        if options is not None and ATTR_AUDIO_OUTPUT in options:
            expected_extension = options[ATTR_AUDIO_OUTPUT]
        else:
            expected_extension = None

        async def get_tts_data() -> str:
            """Handle data available."""
            if engine_instance.name is None:
                raise HomeAssistantError("TTS engine name is not set.")

            if isinstance(engine_instance, Provider):
                extension, data = await engine_instance.async_get_tts_audio(
                    message, language, options
                )
            else:
                extension, data = await engine_instance.internal_async_get_tts_audio(
                    message, language, options
                )

            if data is None or extension is None:
                raise HomeAssistantError(
                    f"No TTS from {engine_instance.name} for '{message}'"
                )

            # Create file infos
            filename = f"{cache_key}.{extension}".lower()

            # Validate filename
            if not _RE_VOICE_FILE.match(filename) and not _RE_LEGACY_VOICE_FILE.match(
                filename
            ):
                raise HomeAssistantError(
                    f"TTS filename '{filename}' from {engine_instance.name} is invalid!"
                )

            # Save to memory
            if extension == "mp3":
                data = self.write_tags(
                    filename, data, engine_instance.name, message, language, options
                )
            self._async_store_to_memcache(cache_key, filename, data)

            if cache:
                self.hass.async_create_task(
                    self._async_save_tts_audio(cache_key, filename, data)
                )

            return filename

        audio_task = self.hass.async_create_task(get_tts_data())

        if expected_extension is None:
            return await audio_task

        def handle_error(_future: asyncio.Future) -> None:
            """Handle error."""
            if audio_task.exception():
                self.mem_cache.pop(cache_key, None)

        audio_task.add_done_callback(handle_error)

        filename = f"{cache_key}.{expected_extension}".lower()
        self.mem_cache[cache_key] = {
            "filename": filename,
            "voice": b"",
            "pending": audio_task,
        }
        return filename

    async def _async_save_tts_audio(
        self, cache_key: str, filename: str, data: bytes
    ) -> None:
        """Store voice data to file and file_cache.

        This method is a coroutine.
        """
        voice_file = os.path.join(self.cache_dir, filename)

        def save_speech() -> None:
            """Store speech to filesystem."""
            with open(voice_file, "wb") as speech:
                speech.write(data)

        try:
            await self.hass.async_add_executor_job(save_speech)
            self.file_cache[cache_key] = filename
        except OSError as err:
            _LOGGER.error("Can't write %s: %s", filename, err)

    async def _async_file_to_mem(self, cache_key: str) -> None:
        """Load voice from file cache into memory.

        This method is a coroutine.
        """
        if not (filename := self.file_cache.get(cache_key)):
            raise HomeAssistantError(f"Key {cache_key} not in file cache!")

        voice_file = os.path.join(self.cache_dir, filename)

        def load_speech() -> bytes:
            """Load a speech from filesystem."""
            with open(voice_file, "rb") as speech:
                return speech.read()

        try:
            data = await self.hass.async_add_executor_job(load_speech)
        except OSError as err:
            del self.file_cache[cache_key]
            raise HomeAssistantError(f"Can't read {voice_file}") from err

        self._async_store_to_memcache(cache_key, filename, data)

    @callback
    def _async_store_to_memcache(
        self, cache_key: str, filename: str, data: bytes
    ) -> None:
        """Store data to memcache and set timer to remove it."""
        self.mem_cache[cache_key] = {
            "filename": filename,
            "voice": data,
            "pending": None,
        }

        @callback
        def async_remove_from_mem(_: datetime) -> None:
            """Cleanup memcache."""
            self.mem_cache.pop(cache_key, None)

        async_call_later(
            self.hass,
            self.time_memory,
            HassJob(
                async_remove_from_mem,
                name="tts remove_from_mem",
                cancel_on_shutdown=True,
            ),
        )

    async def async_read_tts(self, filename: str) -> tuple[str | None, bytes]:
        """Read a voice file and return binary.

        This method is a coroutine.
        """
        if not (record := _RE_VOICE_FILE.match(filename.lower())) and not (
            record := _RE_LEGACY_VOICE_FILE.match(filename.lower())
        ):
            raise HomeAssistantError("Wrong tts file format!")

        cache_key = KEY_PATTERN.format(
            record.group(1), record.group(2), record.group(3), record.group(4)
        )

        if cache_key not in self.mem_cache:
            if cache_key not in self.file_cache:
                raise HomeAssistantError(f"{cache_key} not in cache!")
            await self._async_file_to_mem(cache_key)

        content, _ = mimetypes.guess_type(filename)
        cached = self.mem_cache[cache_key]
        if pending := cached.get("pending"):
            await pending
            cached = self.mem_cache[cache_key]
        return content, cached["voice"]

    @staticmethod
    def write_tags(
        filename: str,
        data: bytes,
        engine_name: str,
        message: str,
        language: str,
        options: dict | None,
    ) -> bytes:
        """Write ID3 tags to file.

        Async friendly.
        """

        data_bytes = io.BytesIO(data)
        data_bytes.name = filename
        data_bytes.seek(0)

        album = engine_name
        artist = language

        if options is not None and (voice := options.get("voice")) is not None:
            artist = voice

        try:
            tts_file = mutagen.File(data_bytes)
            if tts_file is not None:
                if not tts_file.tags:
                    tts_file.add_tags()
                if isinstance(tts_file.tags, ID3):
                    tts_file["artist"] = ID3Text(
                        encoding=3,
                        text=artist,  # type: ignore[no-untyped-call]
                    )
                    tts_file["album"] = ID3Text(
                        encoding=3,
                        text=album,  # type: ignore[no-untyped-call]
                    )
                    tts_file["title"] = ID3Text(
                        encoding=3,
                        text=message,  # type: ignore[no-untyped-call]
                    )
                else:
                    tts_file["artist"] = artist
                    tts_file["album"] = album
                    tts_file["title"] = message
                tts_file.save(data_bytes)
        except mutagen.MutagenError as err:
            _LOGGER.error("ID3 tag error: %s", err)

        return data_bytes.getvalue()


def _init_tts_cache_dir(hass: HomeAssistant, cache_dir: str) -> str:
    """Init cache folder."""
    if not os.path.isabs(cache_dir):
        cache_dir = hass.config.path(cache_dir)
    if not os.path.isdir(cache_dir):
        _LOGGER.info("Create cache dir %s", cache_dir)
        os.mkdir(cache_dir)
    return cache_dir


def _get_cache_files(cache_dir: str) -> dict[str, str]:
    """Return a dict of given engine files."""
    cache = {}

    folder_data = os.listdir(cache_dir)
    for file_data in folder_data:
        if (record := _RE_VOICE_FILE.match(file_data)) or (
            record := _RE_LEGACY_VOICE_FILE.match(file_data)
        ):
            key = KEY_PATTERN.format(
                record.group(1), record.group(2), record.group(3), record.group(4)
            )
            cache[key.lower()] = file_data.lower()
    return cache


class TextToSpeechUrlView(HomeAssistantView):
    """TTS view to get a url to a generated speech file."""

    requires_auth = True
    url = "/api/tts_get_url"
    name = "api:tts:geturl"

    def __init__(self, tts: SpeechManager) -> None:
        """Initialize a tts view."""
        self.tts = tts

    async def post(self, request: web.Request) -> web.Response:
        """Generate speech and provide url."""
        try:
            data = await request.json()
        except ValueError:
            return self.json_message("Invalid JSON specified", HTTPStatus.BAD_REQUEST)
        if (
            not data.get("engine_id")
            and not data.get(ATTR_PLATFORM)
            or not data.get(ATTR_MESSAGE)
        ):
            return self.json_message(
                "Must specify platform and message", HTTPStatus.BAD_REQUEST
            )

        engine = data.get("engine_id") or data[ATTR_PLATFORM]
        message = data[ATTR_MESSAGE]
        cache = data.get(ATTR_CACHE)
        language = data.get(ATTR_LANGUAGE)
        options = data.get(ATTR_OPTIONS)

        try:
            path = await self.tts.async_get_url_path(
                engine, message, cache=cache, language=language, options=options
            )
        except HomeAssistantError as err:
            _LOGGER.error("Error on init tts: %s", err)
            return self.json({"error": err}, HTTPStatus.BAD_REQUEST)

        base = self.tts.base_url or get_url(self.tts.hass)
        url = base + path

        return self.json({"url": url, "path": path})


class TextToSpeechView(HomeAssistantView):
    """TTS view to serve a speech audio."""

    requires_auth = False
    url = "/api/tts_proxy/{filename}"
    name = "api:tts_speech"

    def __init__(self, tts: SpeechManager) -> None:
        """Initialize a tts view."""
        self.tts = tts

    async def get(self, request: web.Request, filename: str) -> web.Response:
        """Start a get request."""
        try:
            content, data = await self.tts.async_read_tts(filename)
        except HomeAssistantError as err:
            _LOGGER.error("Error on load tts: %s", err)
            return web.Response(status=HTTPStatus.NOT_FOUND)

        return web.Response(body=data, content_type=content)


def get_base_url(hass: HomeAssistant) -> str:
    """Get base URL."""
    return hass.data[BASE_URL_KEY] or get_url(hass)


@websocket_api.websocket_command(
    {
        "type": "tts/engine/list",
        vol.Optional("country"): str,
        vol.Optional("language"): str,
    }
)
@callback
def websocket_list_engines(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List text to speech engines and, optionally, if they support a given language."""
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    country = msg.get("country")
    language = msg.get("language")
    providers = []
    provider_info: dict[str, Any]

    for entity in component.entities:
        provider_info = {
            "engine_id": entity.entity_id,
            "supported_languages": entity.supported_languages,
        }
        if language:
            provider_info["supported_languages"] = language_util.matches(
                language, entity.supported_languages, country
            )
        providers.append(provider_info)
    for engine_id, provider in manager.providers.items():
        provider_info = {
            "engine_id": engine_id,
            "supported_languages": provider.supported_languages,
        }
        if language:
            provider_info["supported_languages"] = language_util.matches(
                language, provider.supported_languages, country
            )
        providers.append(provider_info)

    connection.send_message(
        websocket_api.result_message(msg["id"], {"providers": providers})
    )


@websocket_api.websocket_command(
    {
        "type": "tts/engine/get",
        vol.Required("engine_id"): str,
    }
)
@callback
def websocket_get_engine(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Get text to speech engine info."""
    component: EntityComponent[TextToSpeechEntity] = hass.data[DOMAIN]
    manager: SpeechManager = hass.data[DATA_TTS_MANAGER]

    engine_id = msg["engine_id"]
    provider_info: dict[str, Any]

    provider: TextToSpeechEntity | Provider | None = next(
        (entity for entity in component.entities if entity.entity_id == engine_id), None
    )
    if not provider:
        provider = manager.providers.get(engine_id)

    if not provider:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"tts engine {engine_id} not found",
        )
        return

    provider_info = {
        "engine_id": engine_id,
        "supported_languages": provider.supported_languages,
    }

    connection.send_message(
        websocket_api.result_message(msg["id"], {"provider": provider_info})
    )


@websocket_api.websocket_command(
    {
        "type": "tts/engine/voices",
        vol.Required("engine_id"): str,
        vol.Required("language"): str,
    }
)
@callback
def websocket_list_engine_voices(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """List voices for a given language."""
    engine_id = msg["engine_id"]
    language = msg["language"]

    engine_instance = get_engine_instance(hass, engine_id)

    if not engine_instance:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"tts engine {engine_id} not found",
        )
        return

    voices = {"voices": engine_instance.async_get_supported_voices(language)}

    connection.send_message(websocket_api.result_message(msg["id"], voices))
