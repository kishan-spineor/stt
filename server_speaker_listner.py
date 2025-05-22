from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
# from pymongo import MongoClient
# from mongo_conn import transcription_collection
import os
import threading
import queue
import uuid
from google.cloud import speech
from google.cloud import translate_v2 as translate

# === Configuration ===
# os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "prince-speechtotext-3fd7ef9828b8.json"

import json
from google.oauth2 import service_account

# Load the credentials from the env var
credentials_info = json.loads(os.environ['GOOGLE_APPLICATION_CREDENTIALS_JSON'])
credentials = service_account.Credentials.from_service_account_info(credentials_info)
translate_client = translate.Client(credentials=credentials)

app = Flask(__name__)
app.secret_key = 'temporary_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")
# translate_client = translate.Client()

RATE = 48000
clients = {}
transcripts = {}

@app.route('/')
def index():
    return render_template('index.html')

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
    client = speech.SpeechClient()

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

                    # Send to listeners (with translation)
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

                    # Send original to speaker
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

                        # Send to listeners (with translation)
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

                        # Send original to speaker
                        socketio.emit('transcript', {
                            'text': partial_text,
                            'final': False,
                            'translated_text': partial_text,
                            'language': 'en'
                        }, to=sid)

                        previous = transcript_text
        except Exception as e:
            print(f"[ERROR] Streaming failed for sid={sid}: {e}")

    # Start background thread
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

        # final_text = "\n".join(transcripts.get(sid, []))
        # if final_text.strip():
        #     speaker_uuid = clients[sid]['uuid']
        #     filename = f"transcript_{speaker_uuid}.txt"
        #     with open(filename, "a", encoding="utf-8") as f:
        #         f.write(final_text + "\n")
        #     print(f"[TRANSCRIPT SAVED] {filename}")

            # MongoDB logic (optional)
            # result = transcription_collection.update_one(
            #     {'uuid': speaker_uuid},
            #     {'$push': {'transcripts': final_text}},
            #     upsert=True
            # )
            # print(f"[MONGO UPDATE RESULT] {result.raw_result}")
        # else:
        #     print("[WARNING] No transcript to save.")

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

@app.route('/receive_transcript', methods=['POST'])
def receive_transcript():
    data = request.json
    uuid_val = data.get("ssuid")
    text = data.get("text")
    print(f"[API RECEIVED] uuid={uuid_val}, text={text}")
    return jsonify({"status": "received"}), 200



@socketio.on('update_languages')
def handle_update_languages(data):
    sid = request.sid
    new_languages = data.get('languages','hi')
    if sid in clients and clients[sid]['role'] == 'listener':
        clients[sid]['languages'] = new_languages
        print(f"[LANGUAGES UPDATED] sid={sid}, new_languages={new_languages}")


if __name__ == '__main__':
    socketio.run(app, port=5000, debug=True, allow_unsafe_werkzeug=True)
