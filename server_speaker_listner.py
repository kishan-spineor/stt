import os
import json
import uuid
import queue
import threading
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
from google.cloud import speech
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account

# === Load Google Cloud Credentials from JSON stored in env variable ===
raw_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')
if not raw_json:
    raise RuntimeError("Environment variable GOOGLE_APPLICATION_CREDENTIALS_JSON not set")

try:
    credentials_info = json.loads(raw_json)
except json.JSONDecodeError as e:
    raise RuntimeError(f"Failed to decode credentials JSON: {e}")

credentials = service_account.Credentials.from_service_account_info(credentials_info)

# Initialize Google Cloud clients with credentials
translate_client = translate.Client(credentials=credentials)

# === Flask & SocketIO Setup ===
app = Flask(__name__)
app.secret_key = 'temporary_secret_key'  # For production, use a secure random key
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# === Constants and Data Stores ===
RATE = 48000  # Audio sample rate
clients = {}  # sid -> client info dict
transcripts = {}  # sid -> list of finalized transcript strings

# === Socket.IO Event Handlers ===

@socketio.on('connect')
def handle_connect(auth):
    sid = request.sid
    role = auth.get('role', 'listener') if auth else 'listener'
    language = auth.get('language', 'hi') if auth else 'hi'
    print(f"[CONNECTED] sid={sid}, role={role}, language={language}")

    clients[sid] = {
        'role': role,
        'language': language,
        'queue': None,
        'thread': None,
        'uuid': str(uuid.uuid4())
    }
    transcripts[sid] = []

    if role != 'speaker':
        # Listeners don't need further setup
        return

    # Speaker-specific setup
    q = queue.Queue()
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
            chunk = q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    def transcribe_thread():
        try:
            responses = client.streaming_recognize(streaming_config, requests_generator())
            previous = ""
            for response in responses:
                if not response.results:
                    continue
                result = response.results[0]
                if not result.alternatives:
                    continue
                transcript_text = result.alternatives[0].transcript

                if result.is_final:
                    print(f"[FINAL] sid={sid}: {transcript_text}")
                    transcripts[sid].append(transcript_text)
                    full_text = ' '.join(transcripts[sid])

                    # Broadcast final transcript to all listeners, translated
                    for cid, info in clients.items():
                        if info['role'] == 'listener':
                            target_lang = info.get('language', 'hi')
                            try:
                                translation_result = translate_client.translate(
                                    full_text,
                                    source_language='en',
                                    target_language=target_lang
                                )
                                translated_text = translation_result['translatedText']
                            except Exception as e:
                                print(f"[TRANSLATION ERROR] sid={cid}, lang={target_lang}: {e}")
                                translated_text = ""

                            socketio.emit('transcript', {
                                'text': full_text,
                                'final': True,
                                'translated_text': translated_text,
                                'language': target_lang
                            }, to=cid)

                    # Also emit original English final transcript to speaker
                    socketio.emit('transcript', {
                        'text': full_text,
                        'final': True,
                        'translated_text': full_text,
                        'language': 'en'
                    }, to=sid)

                    previous = ""

                else:
                    # Interim partial result - send partial updates only if changed
                    if transcript_text != previous:
                        partial_text = ' '.join(transcripts[sid]) + " " + transcript_text

                        for cid, info in clients.items():
                            if info['role'] == 'listener':
                                target_lang = info.get('language', 'hi')
                                try:
                                    translation_result = translate_client.translate(
                                        partial_text,
                                        source_language='en',
                                        target_language=target_lang
                                    )
                                    translated_text = translation_result['translatedText']
                                except Exception as e:
                                    print(f"[TRANSLATION ERROR] sid={cid}, lang={target_lang}: {e}")
                                    translated_text = ""

                                socketio.emit('transcript', {
                                    'text': partial_text,
                                    'final': False,
                                    'translated_text': translated_text,
                                    'language': target_lang
                                }, to=cid)

                        # Also emit original English partial transcript to speaker
                        socketio.emit('transcript', {
                            'text': partial_text,
                            'final': False,
                            'translated_text': partial_text,
                            'language': 'en'
                        }, to=sid)

                        previous = transcript_text
        except Exception as e:
            print(f"[ERROR] Streaming failed for sid={sid}: {e}")

    # Store queue and thread info for this speaker
    clients[sid]['queue'] = q
    clients[sid]['thread'] = threading.Thread(target=transcribe_thread, daemon=True)
    clients[sid]['thread'].start()


@socketio.on('audio_chunk')
def handle_audio_chunk(chunk):
    """
    Receive raw audio bytes from the speaker and enqueue for transcription.
    """
    sid = request.sid
    if sid in clients and clients[sid]['queue']:
        if chunk:
            clients[sid]['queue'].put(chunk)


@socketio.on('stop_audio')
def handle_stop_audio():
    sid = request.sid
    if sid in clients and clients[sid]['queue']:
        print(f"[STOP AUDIO] sid={sid}")
        clients[sid]['queue'].put(None)  # Signal to stop the generator
        if clients[sid]['thread']:
            clients[sid]['thread'].join()
        clients.pop(sid, None)
        transcripts.pop(sid, None)


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    if sid in clients:
        print(f"[DISCONNECTED] sid={sid}")
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
        print(f"[LANGUAGE UPDATED] sid={sid}, new_language={new_language}")


# REST API endpoint to receive final transcript externally
@app.route('/receive_transcript', methods=['POST'])
def receive_transcript():
    data = request.json
    uuid_val = data.get("ssuid")
    text = data.get("text")
    print(f"[API RECEIVED] uuid={uuid_val}, text={text}")
    return jsonify({"status": "received"}), 200


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
