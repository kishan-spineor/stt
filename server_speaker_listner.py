from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit
import os
import threading
import queue
import uuid
import json
from google.cloud import speech
from google.cloud import translate_v2 as translate
from google.oauth2 import service_account

# === Load Google Cloud Credentials from JSON in environment variable ===
raw_json = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS_JSON')

try:
    credentials_info = json.loads(raw_json)
except json.JSONDecodeError as e:
    print(f"Failed to decode credentials JSON: {e}")
    raise

credentials = service_account.Credentials.from_service_account_info(credentials_info)

# Initialize Google Cloud clients with credentials
translate_client = translate.Client(credentials=credentials)

# === Flask & SocketIO Setup ===
app = Flask(__name__)
app.secret_key = 'temporary_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# === Constants and Data Stores ===
RATE = 48000
clients = {}
transcripts = {}

# === Socket.IO Handlers ===

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
        return

    # Only for speaker
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

                    socketio.emit('transcript', {
                        'text': full_text,
                        'final': True,
                        'translated_text': full_text,
                        'language': 'en'
                    }, to=sid)

                    previous = ""

                else:
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

                        socketio.emit('transcript', {
                            'text': partial_text,
                            'final': False,
                            'translated_text': partial_text,
                            'language': 'en'
                        }, to=sid)

                        previous = transcript_text
        except Exception as e:
            print(f"[ERROR] Streaming failed for sid={sid}: {e}")

    clients[sid]['queue'] = q
    clients[sid]['thread'] = threading.Thread(target=transcribe_thread)
    clients[sid]['thread'].start()

@socketio.on('audio_chunk')
def handle_audio_chunk(data):
    sid = request.sid
    if sid in clients and clients[sid]['queue']:
        chunk = data.get("chunk")
        if chunk:
            clients[sid]['queue'].put(chunk)

@socketio.on('stop_audio')
def handle_stop_audio():
    sid = request.sid
    if sid in clients and clients[sid]['queue']:
        print(f"[STOP AUDIO] sid={sid}")
        clients[sid]['queue'].put(None)
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
            clients[sid]['thread'].join()
        clients.pop(sid, None)
        transcripts.pop(sid, None)

@socketio.on('update_languages')
def handle_update_languages(data):
    sid = request.sid
    new_languages = data.get('languages', 'hi')
    if sid in clients and clients[sid]['role'] == 'listener':
        clients[sid]['language'] = new_languages
        print(f"[LANGUAGES UPDATED] sid={sid}, new_languages={new_languages}")

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
