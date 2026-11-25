import asyncio
import base64
import logging
import os
from typing import Dict, List, Optional

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Request
from ytmusicapi import YTMusic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alexa-ytmusic")

app = FastAPI(title="Alexa YouTube Music Backend")
ytmusic = YTMusic()

COOKIES_PATH = "/tmp/cookies.txt"

# Estado en memoria por usuario/dispositivo
# Estructura: { user_id: { "queue": [track, ...], "index": 0, "offset": 0 } }
PLAYER_STATE: Dict[str, dict] = {}


@app.on_event("startup")
def load_cookies_from_env():
    """Decodifica YOUTUBE_COOKIES_B64 desde las variables de entorno de Azure."""
    cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if cookies_b64:
        try:
            decoded = base64.b64decode(cookies_b64)
            with open(COOKIES_PATH, "wb") as f:
                f.write(decoded)
            logger.info("Cookies de YouTube cargadas correctamente en %s", COOKIES_PATH)
        except Exception as e:
            logger.error("Error decodificando YOUTUBE_COOKIES_B64: %s", e)
    else:
        logger.warning("YOUTUBE_COOKIES_B64 no está configurada. YouTube podría bloquear la IP.")


def extract_audio_url(video_id: str) -> Optional[str]:
    """Extrae la URL directa del stream de audio (M4A/AAC) compatible con Alexa."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if os.path.exists(COOKIES_PATH):
        ydl_opts["cookiefile"] = COOKIES_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("url")
    except Exception as e:
        logger.error("Error extrayendo audio para %s: %s", video_id, e)
        return None


def search_song(query: str) -> Optional[dict]:
    """Busca una canción en YouTube Music y devuelve sus metadatos básicos."""
    results = ytmusic.search(query, filter="songs", limit=5)
    if not results:
        results = ytmusic.search(query, filter="videos", limit=5)
    if not results:
        return None

    item = results[0]
    artists = ", ".join(a["name"] for a in item.get("artists", [])) or "YouTube Music"
    thumbnails = item.get("thumbnails", [])
    art_url = thumbnails[-1]["url"] if thumbnails else None

    return {
        "videoId": item["videoId"],
        "title": item.get("title", query),
        "artist": artists,
        "art_url": art_url,
    }


def populate_related_queue(user_id: str, seed_video_id: str):
    """Carga canciones relacionadas en segundo plano para reproducción continua."""
    try:
        watch = ytmusic.get_watch_playlist(videoId=seed_video_id, limit=15)
        tracks = watch.get("tracks", [])
        state = PLAYER_STATE.get(user_id)
        if not state:
            return

        existing_ids = {t["videoId"] for t in state["queue"]}
        for t in tracks:
            vid = t.get("videoId")
            if not vid or vid in existing_ids:
                continue
            artists = ", ".join(a["name"] for a in t.get("artists", [])) or "YouTube Music"
            thumbs = t.get("thumbnail", [])
            art_url = thumbs[-1]["url"] if thumbs else None
            state["queue"].append(
                {
                    "videoId": vid,
                    "title": t.get("title", "Canción"),
                    "artist": artists,
                    "art_url": art_url,
                }
            )
            existing_ids.add(vid)
        logger.info("Cola actualizada para %s: %d canciones", user_id, len(state["queue"]))
    except Exception as e:
        logger.error("Error cargando cola relacionada: %s", e)


def build_speech_response(text: str, end_session: bool = False) -> dict:
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session,
        },
    }


def build_play_response(
    track: dict,
    stream_url: str,
    speech: Optional[str] = None,
    play_behavior: str = "REPLACE_ALL",
    offset_ms: int = 0,
    expected_previous_token: Optional[str] = None,
) -> dict:
    stream = {
        "token": track["videoId"],
        "url": stream_url,
        "offsetInMilliseconds": offset_ms,
    }
    if play_behavior == "ENQUEUE" and expected_previous_token:
        stream["expectedPreviousToken"] = expected_previous_token

    metadata = {
        "title": track["title"],
        "subtitle": track["artist"],
    }
    if track.get("art_url"):
        metadata["art"] = {"sources": [{"url": track["art_url"]}]}

    response_body: dict = {
        "directives": [
            {
                "type": "AudioPlayer.Play",
                "playBehavior": play_behavior,
                "audioItem": {"stream": stream, "metadata": metadata},
            }
        ]
    }

    if play_behavior == "REPLACE_ALL":
        response_body["shouldEndSession"] = True
        if speech:
            response_body["outputSpeech"] = {"type": "PlainText", "text": speech}

    return {"version": "1.0", "response": response_body}


def build_stop_response(speech: Optional[str] = None) -> dict:
    body: dict = {
        "directives": [{"type": "AudioPlayer.Stop"}],
        "shouldEndSession": True,
    }
    if speech:
        body["outputSpeech"] = {"type": "PlainText", "text": speech}
    return {"version": "1.0", "response": body}


@app.get("/health")
def health():
    return {"status": "ok", "cookies_loaded": os.path.exists(COOKIES_PATH)}


@app.post("/alexa")
async def alexa_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    req = payload.get("request", {})
    req_type = req.get("type", "")
    user_id = (
        payload.get("context", {})
        .get("System", {})
        .get("user", {})
        .get("userId", "default_user")
    )

    logger.info("Evento recibido: %s", req_type)

    # 1. Apertura de la Skill
    if req_type == "LaunchRequest":
        return build_speech_response(
            "YouTube Music listo. ¿Qué canción o artista quieres escuchar?",
            end_session=False,
        )

    # 2. Intents de voz
    if req_type == "IntentRequest":
        intent = req.get("intent", {})
        intent_name = intent.get("name", "")
        slots = intent.get("slots", {})

        if intent_name == "PlaySongIntent":
            query = slots.get("query", {}).get("value")
            if not query:
                return build_speech_response("No entendí qué canción quieres. Intenta de nuevo.")

            track = await asyncio.to_thread(search_song, query)
            if not track:
                return build_speech_response(f"No encontré resultados para {query}.", end_session=True)

            stream_url = await asyncio.to_thread(extract_audio_url, track["videoId"])
            if not stream_url:
                return build_speech_response(
                    "Encontré la canción, pero YouTube bloqueó el audio. Revisa las cookies.",
                    end_session=True,
                )

            PLAYER_STATE[user_id] = {"queue": [track], "index": 0, "offset": 0}
            background_tasks.add_task(populate_related_queue, user_id, track["videoId"])

            return build_play_response(
                track=track,
                stream_url=stream_url,
                speech=f"Reproduciendo {track['title']} de {track['artist']}",
            )

        if intent_name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return build_stop_response()

        if intent_name == "AMAZON.ResumeIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or not state["queue"]:
                return build_speech_response("No hay ninguna canción pausada.", end_session=True)

            track = state["queue"][state["index"]]
            stream_url = await asyncio.to_thread(extract_audio_url, track["videoId"])
            if not stream_url:
                return build_speech_response("No pude reanudar el audio.", end_session=True)

            return build_play_response(
                track=track,
                stream_url=stream_url,
                offset_ms=state.get("offset", 0),
            )

        if intent_name == "AMAZON.NextIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or state["index"] + 1 >= len(state["queue"]):
                return build_speech_response("No hay más canciones en la cola.", end_session=True)

            state["index"] += 1
            state["offset"] = 0
            track = state["queue"][state["index"]]
            stream_url = await asyncio.to_thread(extract_audio_url, track["videoId"])
            if not stream_url:
                return build_speech_response("No pude cargar la siguiente canción.", end_session=True)

            return build_play_response(
                track=track,
                stream_url=stream_url,
                speech=f"Siguiente: {track['title']}",
            )

        if intent_name == "AMAZON.PreviousIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or state["index"] <= 0:
                return build_speech_response("Esta es la primera canción de la cola.", end_session=True)

            state["index"] -= 1
            state["offset"] = 0
            track = state["queue"][state["index"]]
            stream_url = await asyncio.to_thread(extract_audio_url, track["videoId"])
            if not stream_url:
                return build_speech_response("No pude cargar la canción anterior.", end_session=True)

            return build_play_response(track=track, stream_url=stream_url)

        if intent_name == "AMAZON.HelpIntent":
            return build_speech_response(
                "Puedes pedirme cualquier canción diciendo: pon, seguido del nombre o artista.",
                end_session=False,
            )

    # 3. Eventos del AudioPlayer (reproducción continua y estado)
    if req_type == "AudioPlayer.PlaybackNearlyFinished":
        state = PLAYER_STATE.get(user_id)
        current_token = req.get("token")
        if state and state["index"] + 1 < len(state["queue"]):
            next_track = state["queue"][state["index"] + 1]
            stream_url = await asyncio.to_thread(extract_audio_url, next_track["videoId"])
            if stream_url:
                return build_play_response(
                    track=next_track,
                    stream_url=stream_url,
                    play_behavior="ENQUEUE",
                    expected_previous_token=current_token,
                )

    elif req_type == "AudioPlayer.PlaybackStarted":
        state = PLAYER_STATE.get(user_id)
        token = req.get("token")
        if state and token:
            for idx, t in enumerate(state["queue"]):
                if t["videoId"] == token:
                    state["index"] = idx
                    break

    elif req_type == "AudioPlayer.PlaybackStopped":
        state = PLAYER_STATE.get(user_id)
        if state:
            state["offset"] = req.get("offsetInMilliseconds", 0)

    elif req_type == "AudioPlayer.PlaybackFailed":
        logger.error("Error de reproducción en Echo: %s", req.get("error"))

    return {"version": "1.0", "response": {}}
