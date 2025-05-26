import os
import json
import uuid
import queue
import threading
import base64
import logging
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from google.cloud import speech
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account


# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
)

logger = logging.getLogger(__name__)

# === Load Google Cloud Credentials ===
raw_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
if not raw_json:
    raise RuntimeError("Environment variable GOOGLE_APPLICATION_CREDENTIALS_JSON not set")

try:
    credentials_info = json.loads(raw_json)
except json.JSONDecodeError as e:
    raise RuntimeError(f"Failed to decode credentials JSON: {e}")

credentials = service_account.Credentials.from_service_account_info(credentials_info)

translate_client = translate.Client(credentials=credentials)

# === Flask & SocketIO Setup ===
app = Flask(__name__)
# CORS(app, supports_credentials=True)
app.secret_key = 'temporary_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet',transports=["websocket"])

# === Constants and Stores ===
RATE = 48000
clients = {}
transcripts = {}

# === Translation Helper ===
def translate_text(text, target_lang):
    try:
        result = translate_client.translate(text, source_language='en', target_language=target_lang)
        return result.get('translatedText', '')
    except Exception as e:
        logger.error(f"[TRANSLATION ERROR] lang={target_lang}: {e}")
        return ""

# === Socket.IO Handlers ===

@socketio.on('connect')
def handle_connect(auth):
    sid = request.sid
    role = auth.get('role', 'listener') if auth else 'listener'
    language = auth.get('language', 'hi') if auth else 'hi'
    logger.info(f"[CONNECTED] sid={sid}, role={role}, language={language}")

    clients[sid] = {
        'role': role,
        'language': language,
        'queue': None,
        'thread': None,
        'uuid': str(uuid.uuid4())
    }
    transcripts[sid] = []

    if role != 'speaker':
        return

    audio_queue = queue.Queue()
    client = speech.SpeechClient(credentials=credentials)

    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code="en-US",
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        interim_results=True
    )

    def requests_generator():
        while True:
            chunk = audio_queue.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    def transcribe_thread():
        try:
            responses = client.streaming_recognize(streaming_config, requests_generator())
            previous_partial = ""
            for response in responses:
                if not response.results:
                    continue
                result = response.results[0]
                if not result.alternatives:
                    continue
                transcript_text = result.alternatives[0].transcript

                if result.is_final:
                    transcripts[sid].append(transcript_text)
                    full_text = ' '.join(transcripts[sid])
                    logger.info(f"[FINAL TRANSCRIPT] sid={sid}, text={full_text}")

                    for cid, info in clients.items():
                        if info['role'] == 'listener':
                            translated = translate_text(full_text, info.get('language', 'hi'))
                            emit_data = {
                                'text': full_text,
                                'final': True,
                                'translated_text': translated,
                                'language': info.get('language', 'hi')
                            }
                            # logger.info(f"[EMIT TO LISTENER] sid={cid}, data={emit_data}")
                            socketio.emit('transcript', emit_data, to=cid)

                    socketio.emit('transcript', {
                        'text': full_text,
                        'final': True,
                        'translated_text': full_text,
                        'language': 'en'
                    }, to=sid)
                    kishantiwari={
                        'text': full_text,
                        'final': True,
                        'translated_text': full_text,
                        'language': 'en'
                    }
                    logger.info(f"[EMIT TO LISTENER] sid={cid}, data={kishantiwari}")

                    

                    previous_partial = ""

                else:
                    print("else")
                    if transcript_text != previous_partial:
                        partial_text = ' '.join(transcripts[sid]) + " " + transcript_text

                        for cid, info in clients.items():
                            if info['role'] == 'listener':
                                translated = translate_text(partial_text, info.get('language', 'hi'))
                                emit_data = {
                                    'text': partial_text,
                                    'final': False,
                                    'translated_text': translated,
                                    'language': info.get('language', 'hi')
                                }
                                logger.info(f"[EMIT INTERIM TO LISTENER] sid={cid}, data={emit_data}")
                                socketio.emit('transcript', emit_data, to=cid)

                        socketio.emit('transcript', {
                            'text': partial_text,
                            'final': False,
                            'translated_text': partial_text,
                            'language': 'en'
                        }, to=sid)

                        previous_partial = transcript_text

        except Exception as e:
            logger.exception(f"[STREAMING ERROR] sid={sid}")

    clients[sid]['queue'] = audio_queue
    thread = threading.Thread(target=transcribe_thread, daemon=True)
    clients[sid]['thread'] = thread
    thread.start()


@socketio.on('audio_chunk')
def handle_audio_chunk(chunk):
    sid = request.sid
    if sid in clients and clients[sid]['queue']:
        try:
            if isinstance(chunk, str):
                chunk = base64.b64decode(chunk)
            clients[sid]['queue'].put(chunk)
            logger.info(f"[AUDIO CHUNK RECEIVED] sid={sid}, size={len(chunk)} bytes")
        except Exception as e:
            logger.error(f"[AUDIO CHUNK ERROR] sid={sid}, error={e}")


@socketio.on('stop_audio')
def handle_stop_audio():
    sid = request.sid
    logger.info(f"[STOP AUDIO] sid={sid}")
    if sid in clients and clients[sid]['queue']:
        clients[sid]['queue'].put(None)
        if clients[sid]['thread']:
            clients[sid]['thread'].join()
    clients.pop(sid, None)
    transcripts.pop(sid, None)


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.info(f"[DISCONNECTED] sid={sid}")
    if sid in clients:
        if clients[sid]['queue']:
            clients[sid]['queue'].put(None)
            if clients[sid]['thread']:
                clients[sid]['thread'].join()
        clients.pop(sid, None)
        transcripts.pop(sid, None)


@socketio.on('update_languages')
def handle_update_languages(data):
    sid = request.sid
    new_language = data.get('language')
    if sid in clients and clients[sid]['role'] == 'listener' and new_language:
        clients[sid]['language'] = new_language
        logger.info(f"[LANGUAGE UPDATED] sid={sid}, new_language={new_language}")


@app.route('/receive_transcript', methods=['POST'])
def receive_transcript():
    data = request.json
    uuid_val = data.get("ssuid")
    text = data.get("text")
    logger.info(f"[API RECEIVED] uuid={uuid_val}, text={text}")
    return jsonify({"status": "received"}), 200


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)























# import os
# import json
# import uuid
# import queue
# import threading
# from flask import Flask, request, jsonify
# from flask_socketio import SocketIO, emit
# from google.cloud import speech
# from google.cloud import translate_v2 as translate
# from google.oauth2 import service_account
# import base64

# # === Load Google Cloud Credentials from JSON stored in env variable ===
# raw_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
# if not raw_json:
#     raise RuntimeError("Environment variable GOOGLE_APPLICATION_CREDENTIALS_JSON not set")

# try:
#     credentials_info = json.loads(raw_json)
# except json.JSONDecodeError as e:
#     raise RuntimeError(f"Failed to decode credentials JSON: {e}")

# credentials = service_account.Credentials.from_service_account_info(credentials_info)

# # Initialize Google Cloud clients with credentials
# translate_client = translate.Client(credentials=credentials)

# # === Flask & SocketIO Setup ===
# app = Flask(__name__)
# app.secret_key = 'temporary_secret_key'  # Replace with secure key in production
# socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# # === Constants and Data Stores ===
# RATE = 48000  # Audio sample rate in Hz
# clients = {}  # sid -> client info dictionary
# transcripts = {}  # sid -> list of finalized transcript strings

# # === Helper: Translate text with error handling ===
# def translate_text(text, target_lang):
#     try:
#         result = translate_client.translate(text, source_language='en', target_language=target_lang)
#         return result.get('translatedText', '')
#     except Exception as e:
#         print(f"[TRANSLATION ERROR] lang={target_lang}: {e}")
#         return ""

# # === Socket.IO Event Handlers ===

# @socketio.on('connect')
# def handle_connect(auth):
#     sid = request.sid
#     role = auth.get('role', 'listener') if auth else 'listener'
#     language = auth.get('language', 'hi') if auth else 'hi'
#     print(f"[CONNECTED] sid={sid}, role={role}, language={language}")

#     clients[sid] = {
#         'role': role,
#         'language': language,
#         'queue': None,
#         'thread': None,
#         'uuid': str(uuid.uuid4())
#     }
#     transcripts[sid] = []

#     if role != 'speaker':
#         # No further setup needed for listeners
#         return

#     # For speaker role: set up speech streaming client and background thread
#     audio_queue = queue.Queue()
#     client = speech.SpeechClient(credentials=credentials)

#     config = speech.RecognitionConfig(
#         encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
#         sample_rate_hertz=RATE,
#         language_code="en-US",
#     )
#     streaming_config = speech.StreamingRecognitionConfig(
#         config=config,
#         interim_results=True
#     )

#     def requests_generator():
#         while True:
#             chunk = audio_queue.get()
#             if chunk is None:
#                 break
#             yield speech.StreamingRecognizeRequest(audio_content=chunk)

#     def transcribe_thread():
#         try:
#             responses = client.streaming_recognize(streaming_config, requests_generator())
#             previous_partial = ""
#             for response in responses:
#                 if not response.results:
#                     continue
#                 result = response.results[0]
#                 if not result.alternatives:
#                     continue
#                 transcript_text = result.alternatives[0].transcript

#                 if result.is_final:
#                     # Append finalized transcript segment
#                     transcripts[sid].append(transcript_text)
#                     full_text = ' '.join(transcripts[sid])
#                     print(f"[FINAL] sid={sid}: {full_text}")

#                     # Broadcast final transcript (translated) to listeners
#                     for cid, info in clients.items():
#                         if info['role'] == 'listener':
#                             translated = translate_text(full_text, info.get('language', 'hi'))
#                             socketio.emit('transcript', {
#                                 'text': full_text,
#                                 'final': True,
#                                 'translated_text': translated,
#                                 'language': info.get('language', 'hi')
#                             }, to=cid)

#                     # Also send original English final transcript to speaker
#                     socketio.emit('transcript', {
#                         'text': full_text,
#                         'final': True,
#                         'translated_text': full_text,
#                         'language': 'en'
#                     }, to=sid)

#                     previous_partial = ""

#                 else:
#                     # Interim result, send only if changed
#                     if transcript_text != previous_partial:
#                         partial_text = ' '.join(transcripts[sid]) + " " + transcript_text

#                         for cid, info in clients.items():
#                             if info['role'] == 'listener':
#                                 translated = translate_text(partial_text, info.get('language', 'hi'))
#                                 socketio.emit('transcript', {
#                                     'text': partial_text,
#                                     'final': False,
#                                     'translated_text': translated,
#                                     'language': info.get('language', 'hi')
#                                 }, to=cid)

#                         socketio.emit('transcript', {
#                             'text': partial_text,
#                             'final': False,
#                             'translated_text': partial_text,
#                             'language': 'en'
#                         }, to=sid)

#                         previous_partial = transcript_text

#         except Exception as e:
#             print(f"[ERROR] Streaming failed for sid={sid}: {e}")

#     # Save queue and thread info for this speaker client
#     clients[sid]['queue'] = audio_queue
#     thread = threading.Thread(target=transcribe_thread, daemon=True)
#     clients[sid]['thread'] = thread
#     thread.start()




# @socketio.on('audio_chunk')
# def handle_audio_chunk(chunk):
#     sid = request.sid
#     if sid in clients and clients[sid]['queue']:
#         if chunk:
#             # If chunk is base64 encoded string, decode it
#             if isinstance(chunk, str):
#                 try:
#                     chunk = base64.b64decode(chunk)
#                 except Exception as e:
#                     print(f"[AUDIO CHUNK DECODE ERROR] sid={sid}: {e}")
#                     return
#             clients[sid]['queue'].put(chunk)



# @socketio.on('stop_audio')
# def handle_stop_audio():
#     sid = request.sid
#     if sid in clients and clients[sid]['queue']:
#         print(f"[STOP AUDIO] sid={sid}")
#         clients[sid]['queue'].put(None)  # Signal generator to stop
#         if clients[sid]['thread']:
#             clients[sid]['thread'].join()

#         # Clean up after speaker disconnects or stops
#         clients.pop(sid, None)
#         transcripts.pop(sid, None)


# @socketio.on('disconnect')
# def handle_disconnect():
#     sid = request.sid
#     if sid in clients:
#         print(f"[DISCONNECTED] sid={sid}")
#         if clients[sid]['queue']:
#             clients[sid]['queue'].put(None)
#             if clients[sid]['thread']:
#                 clients[sid]['thread'].join()
#         clients.pop(sid, None)
#         transcripts.pop(sid, None)


# @socketio.on('update_languages')
# def handle_update_languages(data):
#     sid = request.sid
#     new_language = data.get('language')
#     if sid in clients and clients[sid]['role'] == 'listener' and new_language:
#         clients[sid]['language'] = new_language
#         print(f"[LANGUAGE UPDATED] sid={sid}, new_language={new_language}")


# # REST API endpoint to receive final transcript externally (optional)
# @app.route('/receive_transcript', methods=['POST'])
# def receive_transcript():
#     data = request.json
#     uuid_val = data.get("ssuid")
#     text = data.get("text")
#     print(f"[API RECEIVED] uuid={uuid_val}, text={text}")
#     return jsonify({"status": "received"}), 200


# if __name__ == '__main__':
#     socketio.run(app, host='0.0.0.0', port=5000, debug=True)
