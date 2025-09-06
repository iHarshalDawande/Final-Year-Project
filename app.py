from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity
import sqlite3
import hashlib
import os
from datetime import datetime, timedelta
import uuid
import threading
import time
import cv2
import numpy as np
import speech_recognition as sr
import pyttsx3
from werkzeug.security import generate_password_hash, check_password_hash
import face_recognition
import json
import logging

app = Flask(__name__)
app.config['JWT_SECRET_KEY'] = 'your-secret-key-change-in-production'
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(days=7)

CORS(app)
jwt = JWTManager(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global variables
pending_requests = {}
camera = None
recognizer = sr.Recognizer()
tts_engine = pyttsx3.init()

# Database initialization
def init_db():
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    
    # Residents table
    c.execute('''CREATE TABLE IF NOT EXISTS residents
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  apartment TEXT UNIQUE NOT NULL,
                  name TEXT NOT NULL,
                  phone TEXT,
                  email TEXT,
                  password_hash TEXT NOT NULL,
                  face_encoding BLOB,
                  voice_signature BLOB,
                  is_owner BOOLEAN DEFAULT 1,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Visitors table
    c.execute('''CREATE TABLE IF NOT EXISTS visitors
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL,
                  phone TEXT,
                  host_apartment TEXT NOT NULL,
                  purpose TEXT,
                  photo_path TEXT,
                  status TEXT DEFAULT 'pending',
                  approved_by TEXT,
                  entry_time TIMESTAMP,
                  exit_time TIMESTAMP,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  expires_at TIMESTAMP)''')
    
    # Access logs
    c.execute('''CREATE TABLE IF NOT EXISTS access_logs
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  person_type TEXT NOT NULL,
                  person_id TEXT,
                  apartment TEXT,
                  access_type TEXT,
                  success BOOLEAN,
                  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  notes TEXT)''')
    
    # System settings
    c.execute('''CREATE TABLE IF NOT EXISTS settings
                 (key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # Pre-approved visitors
    c.execute('''CREATE TABLE IF NOT EXISTS preapproved_visitors
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  apartment TEXT NOT NULL,
                  visitor_name TEXT NOT NULL,
                  visitor_phone TEXT,
                  face_encoding BLOB,
                  valid_from TIMESTAMP NOT NULL,
                  valid_until TIMESTAMP NOT NULL,
                  created_by TEXT NOT NULL,
                  is_active BOOLEAN DEFAULT 1)''')
    
    # Insert demo data
    c.execute("INSERT OR IGNORE INTO residents (apartment, name, phone, password_hash) VALUES (?, ?, ?, ?)",
              ('A-101', 'John Sharma', '9876543210', generate_password_hash('demo123')))
    c.execute("INSERT OR IGNORE INTO residents (apartment, name, phone, password_hash) VALUES (?, ?, ?, ?)",
              ('B-205', 'Priya Patel', '9876543211', generate_password_hash('demo123')))
    
    # Insert system settings
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('security_mode', 'normal')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('visitor_timeout', '20')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('emergency_code', '9999')")
    
    conn.commit()
    conn.close()

# Initialize camera
def init_camera():
    global camera
    try:
        camera = cv2.VideoCapture(0)
        if not camera.isOpened():
            logger.error("Could not open camera")
            camera = None
    except Exception as e:
        logger.error(f"Camera initialization failed: {e}")
        camera = None

# Voice processing functions
def speak(text):
    """Convert text to speech"""
    try:
        tts_engine.say(text)
        tts_engine.runAndWait()
    except Exception as e:
        logger.error(f"TTS error: {e}")

def listen():
    """Listen for voice input"""
    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source)
            audio = recognizer.listen(source, timeout=5)
            text = recognizer.recognize_google(audio)
            return text.lower()
    except sr.UnknownValueError:
        return None
    except sr.RequestError as e:
        logger.error(f"Speech recognition error: {e}")
        return None

# Face recognition functions
def capture_photo():
    """Capture photo from camera"""
    global camera
    if camera is None:
        return None
    
    ret, frame = camera.read()
    if ret:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"visitor_{timestamp}.jpg"
        filepath = os.path.join("static/photos", filename)
        os.makedirs("static/photos", exist_ok=True)
        cv2.imwrite(filepath, frame)
        return filename
    return None

def recognize_face(image_path):
    """Recognize face from image"""
    try:
        # Load and encode the captured image
        image = face_recognition.load_image_file(image_path)
        face_encodings = face_recognition.face_encodings(image)
        
        if not face_encodings:
            return None
            
        # Compare with stored face encodings
        conn = sqlite3.connect('gate_system.db')
        c = conn.cursor()
        c.execute("SELECT apartment, name, face_encoding FROM residents WHERE face_encoding IS NOT NULL")
        residents = c.fetchall()
        conn.close()
        
        for apartment, name, encoding_blob in residents:
            if encoding_blob:
                stored_encoding = np.frombuffer(encoding_blob, dtype=np.float64)
                matches = face_recognition.compare_faces([stored_encoding], face_encodings[0])
                if matches[0]:
                    return {"apartment": apartment, "name": name}
        
        return None
    except Exception as e:
        logger.error(f"Face recognition error: {e}")
        return None

# Security mode functions
def get_security_mode():
    """Get current security mode based on time"""
    current_hour = datetime.now().hour
    if 22 <= current_hour or current_hour < 6:
        return 'late'
    else:
        return 'normal'

# API Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    apartment = data.get('apartment')
    password = data.get('password')
    
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    c.execute("SELECT apartment, name, password_hash FROM residents WHERE apartment = ?", (apartment,))
    user = c.fetchone()
    conn.close()
    
    if user and check_password_hash(user[2], password):
        token = create_access_token(identity=apartment)
        return jsonify({
            'success': True,
            'token': token,
            'user': {'apartment': user[0], 'name': user[1]}
        })
    
    return jsonify({'success': False, 'message': 'Invalid credentials'}), 401

@app.route('/api/visitor-request', methods=['POST'])
def create_visitor_request():
    data = request.get_json()
    
    # Capture photo
    photo_filename = capture_photo()
    
    # Create visitor request
    visitor_id = str(uuid.uuid4())
    expires_at = datetime.now() + timedelta(minutes=20)
    
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    c.execute("""INSERT INTO visitors (name, host_apartment, purpose, photo_path, expires_at, status) 
                 VALUES (?, ?, ?, ?, ?, 'pending')""",
              (data['name'], data['host_apartment'], data['purpose'], photo_filename, expires_at))
    
    visitor_request_id = c.lastrowid
    conn.commit()
    conn.close()
    
    # Add to pending requests
    pending_requests[data['host_apartment']] = {
        'id': visitor_request_id,
        'visitor_name': data['name'],
        'purpose': data['purpose'],
        'photo': photo_filename,
        'expires_at': expires_at
    }
    
    # Voice response
    speak(f"Thank you {data['name']}. I've sent your request to {data['host_apartment']}. Please wait up to 20 minutes for response.")
    
    return jsonify({
        'success': True,
        'message': f"Request sent to {data['host_apartment']}. Please wait for approval.",
        'request_id': visitor_request_id
    })

@app.route('/api/emergency-access', methods=['POST'])
def emergency_access():
    data = request.get_json()
    
    # Log emergency access
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    c.execute("""INSERT INTO access_logs (person_type, person_id, access_type, success, notes) 
                 VALUES ('emergency', ?, 'emergency', 1, ?)""",
              (data.get('service_type', 'unknown'), f"Emergency: {data.get('details', '')}"))
    conn.commit()
    conn.close()
    
    # Voice response
    speak("Emergency access granted. All residents have been notified. Gate opening now.")
    
    return jsonify({
        'success': True,
        'message': 'Emergency access granted',
        'gate_action': 'open'
    })

@app.route('/api/face-recognition', methods=['POST'])
def face_recognition_endpoint():
    # Simulate face recognition for demo
    photo_filename = capture_photo()
    if photo_filename:
        result = recognize_face(f"static/photos/{photo_filename}")
        if result:
            # Log successful access
            conn = sqlite3.connect('gate_system.db')
            c = conn.cursor()
            c.execute("""INSERT INTO access_logs (person_type, person_id, apartment, access_type, success) 
                         VALUES ('resident', ?, ?, 'face_recognition', 1)""",
                      (result['name'], result['apartment']))
            conn.commit()
            conn.close()
            
            speak(f"Welcome home, {result['name']}")
            return jsonify({
                'success': True,
                'resident': result,
                'message': f"Welcome home, {result['name']}",
                'gate_action': 'open'
            })
    
    return jsonify({'success': False, 'message': 'Face not recognized'})

@app.route('/api/pending-requests/<apartment>')
@jwt_required()
def get_pending_requests(apartment):
    if apartment in pending_requests:
        request_data = pending_requests[apartment]
        if datetime.now() < request_data['expires_at']:
            return jsonify({
                'success': True,
                'requests': [request_data]
            })
        else:
            # Request expired
            del pending_requests[apartment]
    
    return jsonify({'success': True, 'requests': []})

@app.route('/api/approve-visitor', methods=['POST'])
@jwt_required()
def approve_visitor():
    data = request.get_json()
    apartment = get_jwt_identity()
    visitor_id = data['visitor_id']
    approved = data['approved']
    
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    
    if approved:
        c.execute("""UPDATE visitors SET status = 'approved', approved_by = ?, entry_time = CURRENT_TIMESTAMP 
                     WHERE id = ?""", (apartment, visitor_id))
        # Remove from pending requests
        if apartment in pending_requests:
            del pending_requests[apartment]
        message = "Visitor approved. Gate opening."
        speak("Access granted. Welcome!")
    else:
        c.execute("UPDATE visitors SET status = 'denied', approved_by = ? WHERE id = ?", (apartment, visitor_id))
        message = "Visitor denied."
        speak("Access denied. Please contact your host.")
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': message})

@app.route('/api/recent-entries/<apartment>')
@jwt_required()
def get_recent_entries(apartment):
    conn = sqlite3.connect('gate_system.db')
    c = conn.cursor()
    c.execute("""SELECT v.name, v.purpose, v.entry_time, v.status 
                 FROM visitors v 
                 WHERE v.host_apartment = ? 
                 ORDER BY v.created_at DESC LIMIT 10""", (apartment,))
    entries = c.fetchall()
    conn.close()
    
    result = []
    for entry in entries:
        result.append({
            'name': entry[0],
            'purpose': entry[1],
            'time': entry[2] or 'Pending',
            'status': entry[3]
        })
    
    return jsonify({'success': True, 'entries': result})

@app.route('/api/delivery-request', methods=['POST'])
def create_delivery_request():
    data = request.get_json()
    
    if data['action'] == 'delivery_room':
        # Generate QR code for delivery room
        qr_code = f"DELIVERY_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        conn = sqlite3.connect('gate_system.db')
        c = conn.cursor()
        c.execute("""INSERT INTO visitors (name, host_apartment, purpose, status) 
                     VALUES (?, ?, 'Delivery - QR: {}', 'delivery_room')""".format(qr_code),
                  (data['name'], data['apartments']))
        conn.commit()
        conn.close()
        
        speak("QR code generated. Please attach the sticker to your packages and proceed to delivery room.")
        
        return jsonify({
            'success': True,
            'message': 'QR code generated for delivery room',
            'qr_code': qr_code
        })
    
    # Regular delivery approval flow
    return create_visitor_request()

@app.route('/api/system-status')
def system_status():
    return jsonify({
        'success': True,
        'status': {
            'security_mode': get_security_mode(),
            'camera_status': 'online' if camera else 'offline',
            'system_time': datetime.now().isoformat(),
            'pending_requests_count': len(pending_requests)
        }
    })

# Cleanup expired requests
def cleanup_expired_requests():
    while True:
        try:
            current_time = datetime.now()
            expired_apartments = []
            
            for apartment, request_data in pending_requests.items():
                if current_time > request_data['expires_at']:
                    expired_apartments.append(apartment)
            
            for apartment in expired_apartments:
                del pending_requests[apartment]
                # Update database
                conn = sqlite3.connect('gate_system.db')
                c = conn.cursor()
                c.execute("UPDATE visitors SET status = 'expired' WHERE host_apartment = ? AND status = 'pending'", (apartment,))
                conn.commit()
                conn.close()
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        time.sleep(60)  # Check every minute

if __name__ == '__main__':
    # Initialize database and camera
    init_db()
    init_camera()
    
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_expired_requests, daemon=True)
    cleanup_thread.start()
    
    # Run Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
