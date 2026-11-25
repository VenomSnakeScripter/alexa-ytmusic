import asyncio
import base64
import logging
import os
import time
from typing import Dict, Optional, Tuple

import httpx
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from ytmusicapi import YTMusic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("alexa-ytmusic")

app = FastAPI(title="Alexa YouTube Music Backend")
ytmusic = YTMusic()

COOKIES_PATH = "/tmp/cookies.txt"

# Estado por usuario: { user_id: { "queue": [track, ...], "index": 0, "offset": 0 } }
PLAYER_STATE: Dict[str, dict] = {}

# Caché de streams: { video_id: (url, headers, expires_at) }
STREAM_CACHE: Dict[str, Tuple[str, dict, float]] = {}


@app.on_event("startup")
def load_cookies_from_env():
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
        logger.warning("YOUTUBE_COOKIES_B64 no está configurada.")


def extract_audio_info(video_id: str) -> Optional[Tuple[str, dict]]:
    """Extrae y cachea la URL directa M4A y sus headers desde YouTube."""
    cached = STREAM_CACHE.get(video_id)
    if cached and cached[2] > time.time():
        return cached[0], cached[1]

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
            stream_url = info.get("url")
            headers = info.get("http_headers", {})
            if stream_url:
                STREAM_CACHE[video_id] = (stream_url, headers, time.time() + 1800)
                return stream_url, headers
            return None
    except Exception as e:
        logger.error("Error extrayendo audio para %s: %s", video_id, e)
        return None


def get_public_base_url(request: Request) -> str:
    """Fuerza HTTPS porque Azure Container Apps termina TLS en el ingress."""
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base


def search_song(query: str) -> Optional[dict]:
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


@app.get("/stream/{video_id}")
async def stream_audio(video_id: str, request: Request):
    """Proxy de audio M4A para que el Echo descargue desde la IP de Azure."""
    info = await asyncio.to_thread(extract_audio_info, video_id)
    if not info:
        raise HTTPException(status_code=502, detail="No se pudo extraer el audio")

    target_url, yt_headers = info
    req_headers = dict(yt_headers)
    if "range" in request.headers:
        req_headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)
    req = client.build_request("GET", target_url, headers=req_headers)
    resp = await client.send(req, stream=True)

    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": resp.headers.get("content-type", "audio/mp4"),
    }
    for h in ("content-length", "content-range"):
        if h in resp.headers:
            response_headers[h.title()] = resp.headers[h]

    async def iter_chunks():
        try:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        iter_chunks(),
        status_code=resp.status_code,
        headers=response_headers,
    )


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
    base_url = get_public_base_url(request)

    logger.info("Evento recibido: %s", req_type)

    if req_type == "LaunchRequest":
        return build_speech_response(
            "YouTube Music listo. ¿Qué canción o artista quieres escuchar?",
            end_session=False,
        )

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

            PLAYER_STATE[user_id] = {"queue": [track], "index": 0, "offset": 0}
            background_tasks.add_task(extract_audio_info, track["videoId"])
            background_tasks.add_task(populate_related_queue, user_id, track["videoId"])

            proxy_url = f"{base_url}/stream/{track['videoId']}"
            return build_play_response(
                track=track,
                stream_url=proxy_url,
                speech=f"Reproduciendo {track['title']} de {track['artist']}",
            )

        if intent_name in ("AMAZON.PauseIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent"):
            return build_stop_response()

        if intent_name == "AMAZON.ResumeIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or not state["queue"]:
                return build_speech_response("No hay ninguna canción pausada.", end_session=True)

            track = state["queue"][state["index"]]
            proxy_url = f"{base_url}/stream/{track['videoId']}"
            return build_play_response(
                track=track,
                stream_url=proxy_url,
                offset_ms=state.get("offset", 0),
            )

        if intent_name == "AMAZON.NextIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or state["index"] + 1 >= len(state["queue"]):
                return build_speech_response("No hay más canciones en la cola.", end_session=True)

            state["index"] += 1
            state["offset"] = 0
            track = state["queue"][state["index"]]
            proxy_url = f"{base_url}/stream/{track['videoId']}"
            return build_play_response(
                track=track,
                stream_url=proxy_url,
                speech=f"Siguiente: {track['title']}",
            )

        if intent_name == "AMAZON.PreviousIntent":
            state = PLAYER_STATE.get(user_id)
            if not state or state["index"] <= 0:
                return build_speech_response("Esta es la primera canción de la cola.", end_session=True)

            state["index"] -= 1
            state["offset"] = 0
            track = state["queue"][state["index"]]
            proxy_url = f"{base_url}/stream/{track['videoId']}"
            return build_play_response(track=track, stream_url=proxy_url)

        if intent_name == "AMAZON.HelpIntent":
            return build_speech_response(
                "Puedes pedirme cualquier canción diciendo: pon, seguido del nombre o artista.",
                end_session=False,
            )

    if req_type == "AudioPlayer.PlaybackNearlyFinished":
        state = PLAYER_STATE.get(user_id)
        current_token = req.get("token")
        if state and state["index"] + 1 < len(state["queue"]):
            next_track = state["queue"][state["index"] + 1]
            proxy_url = f"{base_url}/stream/{next_track['videoId']}"
            return build_play_response(
                track=next_track,
                stream_url=proxy_url,
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
