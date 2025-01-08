import streamlit as st
import pymongo
from bson import ObjectId
from datetime import datetime, timedelta, timezone
import uuid
from io import BytesIO
from audio_recorder_streamlit import audio_recorder
import os
from dotenv import load_dotenv
import hashlib
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
from googleapiclient.errors import HttpError
import urllib.parse
from bson.codec_options import CodecOptions
import time
import qrcode
import base64
from PIL import Image

# Load environment variables
load_dotenv()

def init_google_drive():
    try:
        credentials_path = os.getenv('GOOGLE_DRIVE_CREDENTIALS')
        if not credentials_path:
            st.error("Google Drive credentials path not found in environment variables!")
            st.stop()
            
        if not os.path.exists(credentials_path):
            st.error(f"Credentials file not found at: {credentials_path}")
            st.stop()
            
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        service = build('drive', 'v3', credentials=credentials)
        
        try:
            service.files().list(pageSize=1).execute()
            return service
        except HttpError as e:
            error_msg = str(e)
            if 'accessNotConfigured' in error_msg:
                project_id = None
                import re
                match = re.search(r'project (\d+)', error_msg)
                if match:
                    project_id = match.group(1)
                
                error_text = """
                Google Drive API is not enabled. Please:
                1. Go to Google Cloud Console
                2. Enable the Google Drive API
                3. Wait a few minutes and restart the application
                """
                st.error(error_text)
            else:
                st.error(f"Error accessing Google Drive API: {error_msg}")
            st.stop()
            
    except Exception as e:
        st.error(f"Error initializing Google Drive: {str(e)}")
        st.stop()

def generate_qr_code(url):
    """Generate a QR code for the given URL."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert PIL image to base64 string
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    
    return f"data:image/png;base64,{img_str}"

def generate_unique_patient_id(doctor_id, db):
    """Generate a unique patient ID for a specific doctor."""
    # Get the current highest patient number for this doctor
    latest_assessment = db.assessments.find_one(
        {"doctor_id": doctor_id},
        sort=[("patient_info.patient_id", pymongo.DESCENDING)]
    )
    
    # Extract the number from the latest patient ID or start from 0
    if latest_assessment:
        latest_id = latest_assessment['patient_info']['patient_id']
        try:
            number = int(latest_id.split('-')[1])
        except (IndexError, ValueError):
            number = 0
    else:
        number = 0
    
    # Generate new patient ID
    new_number = number + 1
    return f"P{doctor_id[:4]}-{new_number:04d}"

# Update the GoogleDriveManager class
class GoogleDriveManager:
    def __init__(self, service):
        self.service = service
        self.root_folder_name = "patient_recordings"
        self.root_folder_id = self._get_or_create_folder(self.root_folder_name)
        if not self.root_folder_id:
            raise Exception("Failed to create or access root folder")

    def _get_or_create_folder(self, folder_name, parent_id=None):
        try:
            escaped_name = folder_name.replace("'", "\\'")
            
            query = f"name='{escaped_name}' and mimeType='application/vnd.google-apps.folder'"
            if parent_id:
                query += f" and '{parent_id}' in parents"
            query += " and trashed=false"
            
            results = self.service.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=True
            ).execute()

            if results.get('files'):
                return results['files'][0]['id']
            
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder'
            }
            if parent_id:
                file_metadata['parents'] = [parent_id]
            
            file = self.service.files().create(
                body=file_metadata,
                fields='id',
                supportsAllDrives=True
            ).execute()
            return file.get('id')
            
        except Exception as e:
            st.error(f"Error with folder operations: {str(e)}")
            return None

    def upload_audio(self, audio_data, filename, patient_folder_name):
        try:
            patient_folder_id = self._get_or_create_folder(patient_folder_name, self.root_folder_id)
            if not patient_folder_id:
                raise Exception(f"Failed to create/access folder for patient: {patient_folder_name}")
            
            file_metadata = {
                'name': filename,
                'parents': [patient_folder_id]
            }
            
            if not isinstance(audio_data, bytes):
                raise ValueError("Audio data must be in bytes format")
            
            media = MediaIoBaseUpload(
                io.BytesIO(audio_data),
                mimetype='audio/wav',
                resumable=True,
                chunksize=256 * 1024
            )
            
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute()
            
            if not file.get('id'):
                raise Exception("File upload failed - no file ID received")
            
            return file.get('id')
            
        except Exception as e:
            st.error(f"Error uploading audio: {str(e)}")
            return None
        
def init_mongodb():
    try:
        username = urllib.parse.quote_plus(os.getenv('MONGODB_USERNAME'))
        password = urllib.parse.quote_plus(os.getenv('MONGODB_PASSWORD'))
        cluster = os.getenv('MONGODB_CLUSTER')
        database = os.getenv('MONGODB_DATABASE')
        
        mongo_uri = f"mongodb+srv://{username}:{password}@{cluster}/{database}?retryWrites=true&w=majority"
        
        client = pymongo.MongoClient(mongo_uri)
        db = client[database]
        client.admin.command('ping')
        
        codec_options = CodecOptions(tz_aware=True)
        
        # Initialize collections with validation
        if 'users' not in db.list_collection_names():
            db.create_collection(
                "users",
                validator={
                    "$jsonSchema": {
                        "bsonType": "object",
                        "required": ["email", "password", "role", "created_at"],
                        "properties": {
                            "email": {
                                "bsonType": "string",
                                "pattern": "^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$"
                            },
                            "password": {
                                "bsonType": "string",
                                "minLength": 64
                            },
                            "role": {
                                "enum": ["doctor", "admin"]
                            },
                            "name": {
                                "bsonType": "string"
                            },
                            "created_at": {
                                "bsonType": "date"
                            },
                            "qr_code": {
                                "bsonType": "string"
                            }
                        }
                    }
                }
            )
            users = db.get_collection('users', codec_options=codec_options)
            users.create_index([("email", pymongo.ASCENDING)], unique=True)

        # Initialize assessment_links collection
        if 'assessment_links' not in db.list_collection_names():
            db.create_collection(
                "assessment_links",
                validator={
                    "$jsonSchema": {
                        "bsonType": "object",
                        "required": ["link_id", "doctor_id", "created_at", "expires_at", "used"],
                        "properties": {
                            "link_id": {"bsonType": "string"},
                            "doctor_id": {"bsonType": "string"},
                            "created_at": {"bsonType": "date"},
                            "expires_at": {"bsonType": "date"},
                            "used": {"bsonType": "bool"},
                            "patient_email": {"bsonType": ["string", "null"]},
                            "patient_name": {"bsonType": ["string", "null"]}
                        }
                    }
                }
            )
            assessment_links = db.get_collection('assessment_links', codec_options=codec_options)
            assessment_links.create_index([("link_id", pymongo.ASCENDING)], unique=True)
            assessment_links.create_index([("doctor_id", pymongo.ASCENDING)])
            assessment_links.create_index([("expires_at", pymongo.ASCENDING)])

        if 'assessments' not in db.list_collection_names():
            db.create_collection(
                "assessments",
                validator={
                    "$jsonSchema": {
                        "bsonType": "object",
                        "required": ["assessment_id", "patient_info", "phq9_assessment", "gad7_assessment", "created_at"],
                        "properties": {
                            "assessment_id": {
                                "bsonType": "string"
                            },
                            "patient_info": {
                                "bsonType": "object",
                                "required": ["name", "age", "gender", "language", "education", "email", "clinic", "patient_id", "medication"],
                                "properties": {
                                    "name": { "bsonType": "string" },
                                    "age": { "bsonType": "int" },
                                    "gender": { "enum": ["Male", "Female", "Other"] },
                                    "language": { "bsonType": "string" },
                                    "education": { "bsonType": "string" },
                                    "email": { "bsonType": "string" },
                                    "clinic": { "bsonType": "string" },
                                    "patient_id": { "bsonType": "string" },
                                    "medication": { "enum": ["Yes", "No"] }
                                }
                            },
                            "audio_files": {
                                "bsonType": "object",
                                "properties": {
                                    "animals": { "bsonType": "string" },
                                    "feeling": { "bsonType": "string" },
                                    "image": { "bsonType": "string" },
                                    "counting": { "bsonType": "string" },
                                    "reading": { "bsonType": "string" }
                                }
                            },
                            "phq9_assessment": {
                                "bsonType": "object",
                                "required": ["answers", "score", "severity", "action"],
                                "properties": {
                                    "answers": {
                                        "bsonType": "array",
                                        "items": { "bsonType": "int" },
                                        "minItems": 9,
                                        "maxItems": 9
                                    },
                                    "score": { "bsonType": "int" },
                                    "severity": { "bsonType": "string" },
                                    "action": { "bsonType": "string" }
                                }
                            },
                            "gad7_assessment": {
                                "bsonType": "object",
                                "required": ["answers", "score", "severity"],
                                "properties": {
                                    "answers": {
                                        "bsonType": "array",
                                        "items": { "bsonType": "int" },
                                        "minItems": 7,
                                        "maxItems": 7
                                    },
                                    "score": { "bsonType": "int" },
                                    "severity": { "bsonType": "string" }
                                }
                            },
                            "doctor_id": { "bsonType": "string" },
                            "created_at": { "bsonType": "date" }
                        }
                    }
                }
            )
            assessments = db.get_collection('assessments', codec_options=codec_options)
            assessments.create_index([("assessment_id", pymongo.ASCENDING)], unique=True)
            assessments.create_index([("doctor_id", pymongo.ASCENDING)])
            assessments.create_index([("patient_info.patient_id", pymongo.ASCENDING)])
            assessments.create_index([("created_at", pymongo.ASCENDING)])

        return db

    except Exception as e:
        st.error(f"MongoDB connection error: {str(e)}")
        st.stop()

# Initialize services
db = init_mongodb()
drive_service = init_google_drive()
drive_manager = GoogleDriveManager(drive_service)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

class User:
    def __init__(self, db):
        self.db = db
        self.collection = db.users

    def create_user(self, user_data):
        user_data['password'] = hash_password(user_data['password'])
        user_data['created_at'] = datetime.utcnow()
        try:
            result = self.collection.insert_one(user_data)
            user_id = str(result.inserted_id)
            
            # Generate QR code for doctors
            if user_data['role'] == 'doctor':
                base_url = os.getenv('BASE_URL', 'http://localhost:8501')
                qr_url = f"{base_url}/assessment?doctor={user_id}"
                qr_code = generate_qr_code(qr_url)
                
                # Update user with QR code
                self.collection.update_one(
                    {"_id": result.inserted_id},
                    {"$set": {"qr_code": qr_code}}
                )
            
            return user_id
        except pymongo.errors.DuplicateKeyError:
            return None

    def verify_login(self, email, password):
        user = self.collection.find_one({"email": email})
        if user and hash_password(password) == user['password']:
            return user
        return None


class AssessmentLink:
    def __init__(self, db):
        self.db = db
        self.collection = db.assessment_links

    def create_link(self, doctor_id, expiry_days=7, patient_email=None, patient_name=None):
        link_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(days=expiry_days)
        
        link_data = {
            "link_id": link_id,
            "doctor_id": doctor_id,
            "created_at": created_at,
            "expires_at": expires_at,
            "used": False,
            "patient_email": patient_email,
            "patient_name": patient_name
        }
        
        try:
            self.collection.insert_one(link_data)
            return link_id
        except Exception as e:
            st.error(f"Error creating assessment link: {str(e)}")
            return None

    def validate_link(self, link_id):
        try:
            current_time = datetime.now(timezone.utc)
            link = self.collection.find_one({
                "link_id": link_id,
                "expires_at": {"$gt": current_time},
                "used": False
            })
            return link
        except Exception as e:
            st.error(f"Error validating assessment link: {str(e)}")
            return None

    def mark_link_used(self, link_id):
        try:
            self.collection.update_one(
                {"link_id": link_id},
                {"$set": {"used": True}}
            )
            return True
        except Exception as e:
            st.error(f"Error marking link as used: {str(e)}")
            return False

    def get_doctor_links(self, doctor_id):
        return list(self.collection.find(
            {"doctor_id": doctor_id},
            sort=[("created_at", pymongo.DESCENDING)]
        ))

class Assessment:
    def __init__(self, db):
        self.db = db
        self.collection = db.assessments

    def save_assessment(self, assessment_data):
        try:
            assessment_data['created_at'] = datetime.now(timezone.utc)
            assessment_data['assessment_id'] = str(uuid.uuid4())
            
            if 'patient_info' in assessment_data and 'age' in assessment_data['patient_info']:
                assessment_data['patient_info']['age'] = int(assessment_data['patient_info']['age'])
            
            result = self.collection.insert_one(assessment_data)
            return str(result.inserted_id)
        except Exception as e:
            st.error(f"Error saving assessment: {str(e)}")
            return None

    def get_assessments_by_doctor(self, doctor_id, limit=100):
        return list(self.collection.find(
            {"doctor_id": doctor_id},
            sort=[("created_at", pymongo.DESCENDING)],
            limit=limit
        ))

    def get_patient_history(self, patient_id, limit=100):
        return list(self.collection.find(
            {"patient_info.patient_id": patient_id},
            sort=[("created_at", pymongo.DESCENDING)],
            limit=limit
        ))

    def get_assessment_by_id(self, assessment_id):
        return self.collection.find_one({"assessment_id": assessment_id})

    def get_assessments_by_date_range(self, start_date, end_date, doctor_id=None):
        query = {
            "created_at": {
                "$gte": start_date,
                "$lte": end_date
            }
        }
        if doctor_id:
            query["doctor_id"] = doctor_id
        return list(self.collection.find(query).sort("created_at", pymongo.DESCENDING))

def calculate_phq9_score(answers):
    score = sum(answers)
    if score <= 4:
        severity = "None-minimal"
        action = "None"
    elif score <= 9:
        severity = "Mild"
        action = "Watchful waiting; repeat PHQ-9 at follow-up"
    elif score <= 14:
        severity = "Moderate"
        action = "Treatment plan, considering counseling, follow-up and/or pharmacotherapy"
    elif score <= 19:
        severity = "Moderately Severe"
        action = "Active treatment with pharmacotherapy and/or psychotherapy"
    else:
        severity = "Severe"
        action = "Immediate initiation of pharmacotherapy and expedited referral to mental health specialist"
    
    return score, severity, action

def calculate_gad7_score(answers):
    score = sum(answers)
    if score <= 4:
        severity = "Minimal anxiety"
    elif score <= 9:
        severity = "Mild anxiety"
    elif score <= 14:
        severity = "Moderate anxiety"
    else:
        severity = "Severe anxiety"
    
    return score, severity


def timed_audio_recorder(key, duration, task_description=None, instructions=None):
    """
    Enhanced audio recorder with independent timing for each task
    
    Args:
        key (str): Unique key for the recorder
        duration (int): Recording duration in seconds
        task_description (str): Description of the recording task
        instructions (str): Additional instructions for the user
    """
    # Initialize all session state variables for this recorder if they don't exist
    required_state_vars = {
        f"{key}_recording": False,
        f"{key}_start_time": None,
        f"{key}_recorded_audio": None,
        f"{key}_first_click": True,
        f"{key}_elapsed_time": 0
    }
    
    # Initialize all required state variables if they don't exist
    for var_name, default_value in required_state_vars.items():
        if var_name not in st.session_state:
            st.session_state[var_name] = default_value

    # Create a container for consistent styling
    with st.container():
        # Add task description if provided
        if task_description:
            st.markdown(f"**{task_description}**")
        
        # Add instructions if provided
        if instructions:
            st.markdown(f"<div style='color: #666; font-size: 0.9em; margin-bottom: 10px;'>{instructions}</div>", 
                       unsafe_allow_html=True)

        # Create columns for better layout
        col1, col2, col3 = st.columns([3, 1, 1])
        
        with col1:
            # Enhanced recorder UI
            audio_bytes = audio_recorder(
                key=key,
                recording_color="#FF4B4B",
                neutral_color="#31A852",
                text="" if st.session_state[f"{key}_recording"] else "🎙️ Click to start recording",
                icon_size="2x"
            )
            
            # Handle recording states and timer logic
            if audio_bytes is not None:
                # Recording completed
                st.session_state[f"{key}_recording"] = False
                st.session_state[f"{key}_start_time"] = None
                st.session_state[f"{key}_recorded_audio"] = audio_bytes
                st.session_state[f"{key}_first_click"] = True
                st.session_state[f"{key}_elapsed_time"] = 0
                st.markdown("✅ Recording saved successfully!")
            
            # Start recording when clicked
            elif audio_bytes is None and not st.session_state[f"{key}_recording"] and st.session_state[f"{key}_first_click"]:
                st.session_state[f"{key}_recording"] = True
                st.session_state[f"{key}_start_time"] = time.time()
                st.session_state[f"{key}_first_click"] = False
                st.rerun()
        
        with col2:
            # Timer display
            if st.session_state[f"{key}_recording"] and st.session_state[f"{key}_start_time"] is not None:
                elapsed = time.time() - st.session_state[f"{key}_start_time"]
                st.session_state[f"{key}_elapsed_time"] = elapsed
                remaining = max(0, duration - elapsed)
                progress = min(1.0, elapsed / duration)
                
                # Show countdown timer
                st.markdown(f"⏱️ {int(remaining)}s left")
                st.progress(progress)
                
                # Auto-stop recording if time is up
                if remaining <= 0:
                    st.session_state[f"{key}_recording"] = False
                    st.session_state[f"{key}_start_time"] = None
                    st.session_state[f"{key}_first_click"] = True
                    st.session_state[f"{key}_elapsed_time"] = 0
                    st.warning("⏰ Time's up! Click again to save your recording.")
                    st.rerun()
            else:
                st.markdown(f"⏱️ Duration: {duration}s")
        
        with col3:
            # Status indicator
            if st.session_state[f"{key}_recorded_audio"] is not None:
                st.markdown("📼 Status: **Recorded**")
            elif st.session_state[f"{key}_recording"]:
                st.markdown("🔴 Status: **Recording...**")
            else:
                st.markdown("⚪ Status: **Ready**")
        
        # Add a horizontal line for visual separation
        st.markdown("---")
    
    return st.session_state[f"{key}_recorded_audio"]

def create_dashboard(assessment_manager):
    st.title("Dashboard")
    
    # Get all assessments for the logged-in doctor
    assessments = assessment_manager.get_assessments_by_doctor(st.session_state.user["id"])
    
    # Basic metrics
    total_patients = len({a['patient_info']['patient_id'] for a in assessments})
    
    # Gender distribution
    gender_dist = {}
    for a in assessments:
        gender = a['patient_info']['gender']
        gender_dist[gender] = gender_dist.get(gender, 0) + 1
    
    # Severity distributions
    phq9_dist = {}
    gad7_dist = {}
    for a in assessments:
        phq9_sev = a['phq9_assessment']['severity']
        gad7_sev = a['gad7_assessment']['severity']
        phq9_dist[phq9_sev] = phq9_dist.get(phq9_sev, 0) + 1
        gad7_dist[gad7_sev] = gad7_dist.get(gad7_sev, 0) + 1
    
    # Display metrics
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Patients", total_patients)
    with col2:
        st.metric("Total Assessments", len(assessments))
    with col3:
        st.metric("Avg. PHQ-9 Score", 
                 round(sum(a['phq9_assessment']['score'] for a in assessments) / len(assessments) if assessments else 0, 1))
    
    # Create visualizations using Plotly
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Gender Distribution")
        if gender_dist:
            fig_gender = {
                'data': [{
                    'values': list(gender_dist.values()),
                    'labels': list(gender_dist.keys()),
                    'type': 'pie'
                }],
                'layout': {'height': 400}
            }
            st.plotly_chart(fig_gender, use_container_width=True)
    
    with col2:
        st.subheader("PHQ-9 Severity Distribution")
        if phq9_dist:
            fig_phq9 = {
                'data': [{
                    'x': list(phq9_dist.keys()),
                    'y': list(phq9_dist.values()),
                    'type': 'bar'
                }],
                'layout': {
                    'height': 400,
                    'xaxis': {'tickangle': 45}
                }
            }
            st.plotly_chart(fig_phq9, use_container_width=True)
    
    # Add a button to start new assessment
    if st.button("Start New Assessment", type="primary", use_container_width=True):
        st.session_state.navigation = "New Assessment"
        st.session_state.assessment_step = 1
        st.rerun()

def display_assessment_results(phq9_score, phq9_severity, phq9_action, gad7_score, gad7_severity):
    st.header("Assessment Results")
    
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("PHQ-9 Results")
        st.metric("Score", phq9_score)
        st.info(f"Severity: {phq9_severity}")
        st.write(f"**Recommended Action:** {phq9_action}")
    
    with col2:
        st.subheader("GAD-7 Results")
        st.metric("Score", gad7_score)
        st.info(f"Severity: {gad7_severity}")
        
def create_audio_assessment_section():
    """
    Creates an improved audio assessment section with clear instructions and better UI
    """
    st.header("Voice Assessment Tasks")
    st.markdown("""
    <div style='background-color: #f0f7ff; padding: 15px; border-radius: 5px; margin-bottom: 20px;'>
        <h4 style='color: #1e88e5; margin-top: 0;'>Instructions</h4>
        <ul style='margin-bottom: 0;'>
            <li>Please complete each recording task in a quiet environment</li>
            <li>Speak clearly and at a normal pace</li>
            <li>You can re-record if you're not satisfied with your recording</li>
            <li>Wait for the timer to complete or click again to stop recording</li>
        </ul>
    </div>
    """, unsafe_allow_html=True)

    # Task 1: Animal Names
    animals_audio = timed_audio_recorder(
        "animals", 
        60,
        "Task 1: Animal Names",
        "Name as many different animals as you can think of. Try to be as specific as possible."
    )

    # Task 2: Feelings Description
    feeling_audio = timed_audio_recorder(
        "feeling", 
        120,
        "Task 2: Current Feelings",
        "Describe how you've been feeling lately, both emotionally and physically."
    )

    # Task 3: Image Description
    st.markdown("### Task 3: Image Description")
    # Display the cookie theft image
    st.image(
    "/Users/hemantgoyal/Downloads/Freelancing/Active Clients/Roopak/Project1/Yugaya scene.jpg", 
    caption="Cookie Theft Picture",
    width=300  # Set a specific width in pixels
)

    
    image_audio = timed_audio_recorder(
        "image", 
        60,
        "Task 3: Image Description",
        "Look at the image above and describe what you see in detail. Include the actions, people, and objects you observe."
    )

    # Task 4: Counting
    counting_audio = timed_audio_recorder(
        "counting", 
        30,
        "Task 4: Number Sequence",
        "Count from 1 to 20 at a steady pace."
    )

    # Task 5: Reading
    reading_text = """
    The rainbow is a magnificent natural phenomenon that appears in the sky when sunlight and rain combine in a very specific way. 
    It takes the form of a multicolored circular arc, consisting of seven colors: red, orange, yellow, green, blue, indigo, and violet. 
    Each of these colors emerges when sunlight is refracted and reflected by water droplets in the atmosphere.
    """
    
    st.markdown("### Task 5: Reading Task")
    st.markdown(f"```\n{reading_text}\n```")
    
    reading_audio = timed_audio_recorder(
        "reading", 
        120,
        "Task 5: Text Reading",
        "Read the text passage above aloud clearly and at a comfortable pace."
    )

    # Return all audio recordings
    return {
        'animals': animals_audio,
        'feeling': feeling_audio,
        'image': image_audio,
        'counting': counting_audio,
        'reading': reading_audio
    }
def create_assessment_form(assessment_manager, link_data=None, doctor_id=None):
    if 'assessment_step' not in st.session_state:
        st.session_state.assessment_step = 1
    
    if 'form_data' not in st.session_state:
        st.session_state.form_data = {}
    
    if 'audio_recordings' not in st.session_state:
        st.session_state.audio_recordings = {}
    
    # Generate patient ID if not already present in form_data
    if doctor_id and 'patient_id' not in st.session_state.form_data:
        st.session_state.form_data['patient_id'] = generate_unique_patient_id(doctor_id, db)
    elif st.session_state.user and 'patient_id' not in st.session_state.form_data:
        # For cases when doctor is logged in directly
        st.session_state.form_data['patient_id'] = generate_unique_patient_id(st.session_state.user["id"], db)
    
    st.title("Patient Assessment Form")
    
    if link_data:
        st.info("This form was shared by your healthcare provider.")
    
    progress = (st.session_state.assessment_step - 1) / 4
    st.progress(progress)
    # Add custom CSS to improve radio button visibility
    st.markdown("""
        <style>
        /* Improve radio button visibility */
        .stRadio > div {
            gap: 1rem;
            padding: 0.5rem;
        }
        
        .stRadio > div > div > label {
            background-color: #f8f9fa;
            padding: 0.5rem 1rem;
            border-radius: 0.25rem;
            border: 1px solid #dee2e6;
            margin-bottom: 0.5rem;
            width: 100%;
            display: flex;
            align-items: center;
        }
        
        .stRadio > div > div > label:hover {
            background-color: #e9ecef;
        }
        
        /* Fix radio button alignment */
        .stRadio > div > div > label > div {
            margin-right: 0.5rem;
        }
        
        /* Style the text input fields */
        .stTextInput > div > div > input {
            border-radius: 0.25rem;
        }
        
        /* Style the number input field */
        .stNumberInput > div > div > input {
            border-radius: 0.25rem;
        }
        </style>
    """, unsafe_allow_html=True)
    
    # Step 1: Personal Information
    if st.session_state.assessment_step == 1:
        st.header("Personal Information")
        col1, col2 = st.columns(2)
        
        with col1:
            name = st.text_input("Full Name", 
                               value=st.session_state.form_data.get('name', link_data.get("patient_name", "") if link_data else ""))
            age = st.number_input("Age in years", min_value=0, max_value=120,
                                value=st.session_state.form_data.get('age', 0))
            gender = st.radio("Gender", ["Male", "Female", "Other"],
                            index=["Male", "Female", "Other"].index(st.session_state.form_data.get('gender', "Male")))
            language = st.radio("Native language", ["English", "Hindi", "Telugu", "Other"],
                              index=["English", "Hindi", "Telugu", "Other"].index(st.session_state.form_data.get('language', "English")))
        
        with col2:
            education = st.radio("Highest formal education", 
                               ["Secondary school", "Intermediate", "Graduation", "Post-graduation"])
            email = st.text_input("Email ID", 
                                value=st.session_state.form_data.get('email', link_data.get("patient_email", "") if link_data else ""))
            clinic = st.text_input("Clinic (or) doctor name",
                                 value=st.session_state.form_data.get('clinic', ""))
            
            # Display auto-generated patient ID (read-only)
            st.text_input(
                "Patient ID (Auto-generated)",
                value=st.session_state.form_data.get('patient_id', ''),
                disabled=True
            )
            
            medication = st.radio("On medication for mental health condition", ["Yes", "No"])
        
        if st.button("Next: Voice Assessment"):
            required_fields = {
                'name': name,
                'age': age if age > 0 else None,
                'gender': gender,
                'language': language,
                'education': education,
                'email': email,
                'clinic': clinic,
                'medication': medication
            }
            
            missing_fields = [field for field, value in required_fields.items() if not value]
            
            if missing_fields:
                st.error(f"Please fill in the following fields: {', '.join(missing_fields)}")
                return
            
            # Save all form data including patient_id
            st.session_state.form_data.update({
                'name': name,
                'age': age,
                'gender': gender,
                'language': language,
                'education': education,
                'email': email,
                'clinic': clinic,
                'medication': medication
            })
            
            st.session_state.assessment_step = 2
            st.rerun()
            
    
    # Step 2: Voice Assessments
    elif st.session_state.assessment_step == 2:
        audio_recordings = create_audio_assessment_section()
        
        # Store recordings in session state
        if any(audio_recordings.values()):
            st.session_state.audio_recordings = audio_recordings
        
        # Navigation buttons
        col1, col2 = st.columns(2)
        with col1:
            if st.button("⬅️ Previous: Personal Info"):
                st.session_state.assessment_step = 1
                st.rerun()
        with col2:
            if st.button("Next: PHQ-9 Assessment ➡️"):
                if not any(st.session_state.audio_recordings.values()):
                    st.error("Please complete at least one audio recording before proceeding.")
                    return
                st.session_state.assessment_step = 3
                st.rerun()
    
    # Step 3: PHQ-9 Assessment
    elif st.session_state.assessment_step == 3:
        st.header("PHQ-9 Assessment")
        st.write("Over the last 2 weeks, how often have you been bothered by any of the following problems?")
        
        phq9_questions = [
            "Little interest or pleasure in doing things",
            "Feeling down, depressed, or hopeless",
            "Trouble falling or staying asleep, or sleeping too much",
            "Feeling tired or having little energy",
            "Poor appetite or overeating",
            "Feeling bad about yourself or that you are a failure",
            "Trouble concentrating on things",
            "Moving or speaking so slowly that other people could have noticed",
            "Thoughts that you would be better off dead or of hurting yourself"
        ]
        
        options = ["Not at all", "Several days", "More than half the days", "Nearly every day"]
        phq9_answers = []
        
        for i, q in enumerate(phq9_questions):
            st.write(f"**{i+1}. {q}**")
            answer = st.radio(
                f"Question {i+1}",
                options,
                key=f"phq9_{i}",
                index=st.session_state.form_data.get(f'phq9_{i}', 0),
                horizontal=True,
                label_visibility="collapsed"
            )
            phq9_answers.append(options.index(answer))
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Previous: Voice Assessment"):
                st.session_state.assessment_step = 2
                st.rerun()
        with col2:
            if st.button("Next: GAD-7 Assessment"):
                # Save PHQ-9 answers
                for i, answer in enumerate(phq9_answers):
                    st.session_state.form_data[f'phq9_{i}'] = answer
                st.session_state.assessment_step = 4
                st.rerun()
    
    # Step 4: GAD-7 Assessment
    else:
        st.header("GAD-7 Assessment")
        st.write("Over the last 2 weeks, how often have you been bothered by the following problems?")
        
        gad7_questions = [
            "Feeling nervous, anxious or on edge",
            "Not being able to stop or control worrying",
            "Worrying too much about different things",
            "Trouble relaxing",
            "Being so restless that is hard to sit still",
            "Becoming easily annoyed or irritable",
            "Feeling afraid as if something awful might happen"
        ]
        
        options = ["Not at all", "Several days", "More than half the days", "Nearly every day"]
        gad7_answers = []
        
        for i, q in enumerate(gad7_questions):
            st.write(f"**{i+1}. {q}**")
            answer = st.radio(
                f"Question {i+1}",
                options,
                key=f"gad7_{i}",
                index=st.session_state.form_data.get(f'gad7_{i}', 0),
                horizontal=True,
                label_visibility="collapsed"
            )
            gad7_answers.append(options.index(answer))
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Previous: PHQ-9 Assessment"):
                st.session_state.assessment_step = 3
                st.rerun()
        with col2:
            if st.button("Submit Assessment"):
                # Ensure we have the patient_id
                if 'patient_id' not in st.session_state.form_data:
                    if doctor_id:
                        st.session_state.form_data['patient_id'] = generate_unique_patient_id(doctor_id, db)
                    elif st.session_state.user:
                        st.session_state.form_data['patient_id'] = generate_unique_patient_id(st.session_state.user["id"], db)
                
                # Calculate scores
                phq9_answers = [st.session_state.form_data[f'phq9_{i}'] for i in range(9)]
                phq9_score, phq9_severity, phq9_action = calculate_phq9_score(phq9_answers)
                gad7_score, gad7_severity = calculate_gad7_score(gad7_answers)

                # Create patient folder name using the stored patient_id
                patient_folder_name = f"{st.session_state.form_data['name']}_{st.session_state.form_data['patient_id']}".replace(" ", "_")

                # Prepare assessment data
                assessment_data = {
                    "patient_info": {
                        "name": st.session_state.form_data['name'],
                        "age": st.session_state.form_data['age'],
                        "gender": st.session_state.form_data['gender'],
                        "language": st.session_state.form_data['language'],
                        "education": st.session_state.form_data['education'],
                        "email": st.session_state.form_data['email'],
                        "clinic": st.session_state.form_data['clinic'],
                        "patient_id": st.session_state.form_data['patient_id'],
                        "medication": st.session_state.form_data['medication']
                    },
                    "audio_files": {},
                    "phq9_assessment": {
                        "answers": phq9_answers,
                        "score": phq9_score,
                        "severity": phq9_severity,
                        "action": phq9_action
                    },
                    "gad7_assessment": {
                        "answers": gad7_answers,
                        "score": gad7_score,
                        "severity": gad7_severity
                    },
                    "doctor_id": doctor_id if doctor_id else st.session_state.user.get("id")
                }


                # Upload audio files
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                upload_success = True

                for audio_type, audio_data in st.session_state.audio_recordings.items():
                    if audio_data:
                        try:
                            filename = f"{audio_type}_{timestamp}.wav"
                            file_id = drive_manager.upload_audio(
                                audio_data,
                                filename,
                                patient_folder_name
                            )
                            if file_id:
                                assessment_data["audio_files"][audio_type] = file_id
                            else:
                                st.error(f"Failed to upload {audio_type} audio file")
                                upload_success = False
                                break
                        except Exception as e:
                            st.error(f"Error uploading {audio_type} audio: {str(e)}")
                            upload_success = False
                            break

                if not upload_success:
                    return

                # Save assessment
                result = assessment_manager.save_assessment(assessment_data)
                
                if result:
                    if link_data:
                        link_manager = AssessmentLink(db)
                        link_manager.mark_link_used(link_data["link_id"])

                    # Clear session state
                    st.session_state.audio_recordings = {}
                    st.session_state.form_data = {}
                    
                    st.success("Assessment submitted successfully!")
                    display_assessment_results(
                        phq9_score, phq9_severity, phq9_action,
                        gad7_score, gad7_severity
                    )
                    
                    # Reset form
                    st.session_state.assessment_step = 1
                    st.session_state.page = "dashboard"
                    st.rerun()
                else:
                    st.error("Failed to save assessment")

    # Display current step indicator
    st.sidebar.markdown("---")
    st.sidebar.write("Current Progress:")
    steps = ["Personal Information", "Voice Assessment", "PHQ-9 Assessment", "GAD-7 Assessment"]
    current_step = steps[st.session_state.assessment_step - 1]
    
    for i, step in enumerate(steps, 1):
        if i < st.session_state.assessment_step:
            st.sidebar.success(f"✓ Step {i}: {step}")
        elif i == st.session_state.assessment_step:
            st.sidebar.info(f"→ Step {i}: {step}")
        else:
            st.sidebar.write(f"Step {i}: {step}")
            
    # Add warning for unsaved changes
    st.sidebar.markdown("---")
    st.sidebar.warning("Please complete all steps before leaving the form to avoid losing your progress.")
    
def display_doctor_qr(user_id):
    st.header("Your Patient Registration QR Code")
    
    user = db.users.find_one({"_id": ObjectId(user_id)})
    if user and 'qr_code' in user:
        st.markdown(f'<img src="{user["qr_code"]}" width="300">', unsafe_allow_html=True)
        st.info("Share this QR code with your patients. When scanned, it will direct them to the assessment form.")
        
        # Add download button
        qr_data = user['qr_code'].split(',')[1]
        qr_bytes = base64.b64decode(qr_data)
        st.download_button(
            label="Download QR Code",
            data=qr_bytes,
            file_name="doctor_qr.png",
            mime="image/png",
            key="download_qr"
        )
    else:
        st.error("QR code not found. Please contact support.")

    
def manage_assessment_links(link_manager):
    st.title("Manage Assessment Links")
    
    st.header("Create New Assessment Link")
    col1, col2 = st.columns(2)
    
    with col1:
        patient_name = st.text_input("Patient Name (optional)")
        patient_email = st.text_input("Patient Email (optional)")
    
    with col2:
        expiry_days = st.number_input("Link valid for (days)", min_value=1, max_value=30, value=7)
    
    if st.button("Generate Link"):
        link_id = link_manager.create_link(
            st.session_state.user["id"],
            expiry_days,
            patient_email,
            patient_name
        )
        if link_id:
            # Simply use the link_id as the URL parameter
            link_url = f"assessment?link={link_id}"
            st.success("Assessment link created!")
            st.code(link_url)
            
            # Create a button to copy the link
            if st.button("Copy Link", key="copy_new_link"):
                st.write("Link copied to clipboard!")

    st.header("Active Assessment Links")
    links = link_manager.get_doctor_links(st.session_state.user["id"])
    
    current_time = datetime.now(timezone.utc)
    
    if links:
        for link in links:
            with st.expander(f"Link for {link.get('patient_name', 'Anonymous')} - {link['created_at'].strftime('%Y-%m-%d')}"):
                st.write(f"**Status:** {'Used' if link['used'] else 'Active'}")
                st.write(f"**Expires:** {link['expires_at'].strftime('%Y-%m-%d %H:%M')} UTC")
                if link.get('patient_email'):
                    st.write(f"**Patient Email:** {link['patient_email']}")
                
                # Make sure expires_at is timezone aware
                expires_at = link['expires_at']
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if not link['used'] and expires_at > current_time:
                    link_url = f"assessment?link={link['link_id']}"
                    st.code(link_url)
                    
                    # Create a unique button for each link
                    if st.button("Copy", key=f"copy_{link['link_id']}"):
                        st.write("Link copied to clipboard!")
    else:
        st.info("No assessment links created")

def view_assessments(assessment_manager):
    st.title("View Assessments")
    
    filter_option = st.selectbox(
        "Filter by",
        ["All", "Patient ID", "Date Range"]
    )
    
    assessments = []
    if filter_option == "Patient ID":
        patient_id = st.text_input("Enter Patient ID")
        if patient_id:
            assessments = assessment_manager.get_patient_history(patient_id)
    elif filter_option == "Date Range":
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Start Date")
        with col2:
            end_date = st.date_input("End Date")
        
        if start_date and end_date:
            start_datetime =datetime.combine(start_date, datetime.min.time())
            end_datetime = datetime.combine(end_date, datetime.max.time())
            assessments = assessment_manager.get_assessments_by_date_range(
                start_datetime,
                end_datetime,
                st.session_state.user["id"]
            )
    else:
        assessments = assessment_manager.get_assessments_by_doctor(st.session_state.user["id"])
    
    if assessments:
        for assessment in assessments:
            with st.expander(f"Assessment for {assessment['patient_info']['name']} - {assessment['created_at'].strftime('%Y-%m-%d %H:%M')}"):
                col1, col2 = st.columns(2)
                
                with col1:
                    st.subheader("Patient Information")
                    for key, value in assessment['patient_info'].items():
                        st.write(f"**{key.title()}:** {value}")
                
                with col2:
                    st.subheader("Assessment Scores")
                    
                    st.write("##### PHQ-9 Assessment")
                    st.metric("Score", assessment['phq9_assessment']['score'])
                    st.info(f"Severity: {assessment['phq9_assessment']['severity']}")
                    st.write(f"Action: {assessment['phq9_assessment']['action']}")
                    
                    st.write("##### GAD-7 Assessment")
                    st.metric("Score", assessment['gad7_assessment']['score'])
                    st.info(f"Severity: {assessment['gad7_assessment']['severity']}")
                
                if 'audio_files' in assessment and assessment['audio_files']:
                    st.subheader("Audio Recordings")
                    cols = st.columns(len(assessment['audio_files']))
                    for idx, (audio_type, file_id) in enumerate(assessment['audio_files'].items()):
                        with cols[idx]:
                            st.write(f"**{audio_type.title()}**")
                            st.link_button(
                                "Open in Drive",
                                f"https://drive.google.com/file/d/{file_id}/view",
                                use_container_width=True
                            )
    else:
        st.info("No assessments found for the selected criteria")
def signup_page(user_manager):
    # Center the content
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        # Add logo
        st.image("/Users/hemantgoyal/Downloads/Freelancing/Active Clients/Roopak/Project1/horizontal logo.png", width=300)
        
        # Create a card-like container for signup
        st.markdown("""
            <style>
            .auth-container {
                background-color: white;
                padding: 2rem;
                border-radius: 1rem;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
                margin-top: 2rem;
            }
            
            .auth-title {
                color: #1F2937;
                font-family: 'Inter', sans-serif;
                font-size: 1.5rem;
                font-weight: 600;
                margin-bottom: 1.5rem;
                text-align: center;
            }
            
            /* Override Streamlit's default background */
            .stApp {
                background-color: #F3F4F6;
            }
            
            div[data-testid="stForm"] {
                background-color: white;
                padding: 2rem;
                border-radius: 1rem;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }
            
            /* Style form inputs */
            div[data-testid="stForm"] input {
                background-color: #F9FAFB;
                border: 1px solid #E5E7EB;
                border-radius: 0.5rem;
                padding: 0.75rem;
                width: 100%;
                margin-bottom: 1rem;
            }
            
            /* Style select boxes */
            div[data-testid="stSelectbox"] > div > div {
                background-color: #F9FAFB;
                border: 1px solid #E5E7EB;
                border-radius: 0.5rem;
                padding: 0.5rem;
            }
            
            /* Style buttons */
            .stButton > button {
                background-color: #4F46E5;
                color: white;
                padding: 0.75rem 1.5rem;
                border-radius: 0.5rem;
                border: none;
                width: 100%;
                font-weight: 500;
                margin-top: 1rem;
            }
            
            .stButton > button:hover {
                background-color: #4338CA;
                transform: translateY(-1px);
            }
            
            /* Secondary button */
            .secondary-button > button {
                background-color: #9CA3AF;
            }
            
            .secondary-button > button:hover {
                background-color: #6B7280;
            }
            </style>
        """, unsafe_allow_html=True)
        
        with st.form("signup_form"):
            st.markdown('<p class="auth-title">Create Account</p>', unsafe_allow_html=True)
            
            name = st.text_input("Full Name")
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            role = st.selectbox("Role", ["doctor", "admin"])
            submitted = st.form_submit_button("Create Account")
            
            if submitted:
                if not all([name, email, password, role]):
                    st.error("Please fill in all fields")
                    return
                
                user_data = {
                    "name": name,
                    "email": email,
                    "password": password,
                    "role": role
                }
                
                user_id = user_manager.create_user(user_data)
                if user_id:
                    st.success("Account created successfully! Please login.")
                    st.session_state.show_signup = False
                    st.rerun()
                else:
                    st.error("Email already exists")

        # Back to login button with secondary styling
        st.markdown(
            """
            <style>
            div[data-testid="button-key-1"] {
                background-color: #9CA3AF !important;
            }
            div[data-testid="button-key-1"]:hover {
                background-color: #6B7280 !important;
            }
            </style>
            """,
            unsafe_allow_html=True
        )
        if st.button("Back to login", key="back_to_login"):
            st.session_state.show_signup = False
            st.rerun()

def login_page(user_manager):
    # Center the content
    col1, col2, col3 = st.columns([1, 2, 1])
    
    with col2:
        # Add logo
        st.image("/Users/hemantgoyal/Downloads/Freelancing/Active Clients/Roopak/Project1/horizontal logo.png", width=300)
        
        with st.form("login_form"):
            st.markdown('<p class="auth-title">Login</p>', unsafe_allow_html=True)
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login")
            
            if submitted:
                user = user_manager.verify_login(email, password)
                if user:
                    st.session_state.authenticated = True
                    st.session_state.user = {
                        "id": str(user["_id"]),
                        "email": user["email"],
                        "role": user["role"],
                        "name": user.get("name", "")
                    }
                    st.success("Login successful!")
                    st.rerun()
                else:
                    st.error("Invalid email or password")

        if st.button("Create new account"):
            st.session_state.show_signup = True
            st.rerun()

def main():
    st.set_page_config(
        page_title="Mental Health Assessment System",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    
    st.markdown(f"<style>{MODERN_THEME}</style>", unsafe_allow_html=True)
    
    
    
    # Initialize managers
    user_manager = User(db)
    assessment_manager = Assessment(db)
    link_manager = AssessmentLink(db)
    
    # Initialize session state
    if 'authenticated' not in st.session_state:
        st.session_state.authenticated = False
    if 'user' not in st.session_state:
        st.session_state.user = None
    if 'show_signup' not in st.session_state:
        st.session_state.show_signup = False
    if 'navigation' not in st.session_state:
        st.session_state.navigation = "Dashboard"
    
    # Check for assessment link or doctor parameter
    query_params = st.query_params
    if 'link' in query_params:
        link_id = query_params['link']
        link_data = link_manager.validate_link(link_id)
        if link_data:
            doctor = db.users.find_one({"_id": ObjectId(link_data["doctor_id"])})
            if doctor:
                link_data["doctor_name"] = doctor.get("name", "your doctor")
                create_assessment_form(assessment_manager, link_data, link_data["doctor_id"])
            else:
                st.error("Invalid assessment link")
        else:
            st.error("This assessment link has expired or has already been used")
        return
    elif 'doctor' in query_params:
        doctor_id = query_params['doctor']
        try:
            doctor = db.users.find_one({"_id": ObjectId(doctor_id)})
            if doctor and doctor['role'] == 'doctor':
                create_assessment_form(assessment_manager, doctor_id=doctor_id)
                return
            else:
                st.error("Invalid doctor reference")
        except:
            st.error("Invalid doctor reference")
        return
    
    # Regular application flow
    if not st.session_state.authenticated:
        if st.session_state.show_signup:
            signup_page(user_manager)
        else:
            login_page(user_manager)
    else:
        # Top navigation
        st.title(f"Welcome, {st.session_state.user['name']}")
        
        # Create navigation tabs with QR Code option for doctors
        if st.session_state.user['role'] == 'doctor':
            nav_cols = st.columns(5)
            with nav_cols[0]:
                if st.button("Dashboard", key="nav_dashboard", use_container_width=True):
                    st.session_state.navigation = "Dashboard"
                    st.rerun()
            with nav_cols[1]:
                if st.button("View Assessments", key="nav_view", use_container_width=True):
                    st.session_state.navigation = "View Assessments"
                    st.rerun()
            with nav_cols[2]:
                if st.button("Manage Links", key="nav_links", use_container_width=True):
                    st.session_state.navigation = "Manage Links"
                    st.rerun()
            with nav_cols[3]:
                if st.button("My QR Code", key="nav_qr", use_container_width=True):
                    st.session_state.navigation = "QR Code"
                    st.rerun()
            with nav_cols[4]:
                if st.button("Logout", key="nav_logout", use_container_width=True):
                    st.session_state.authenticated = False
                    st.session_state.user = None
                    st.session_state.navigation = "Dashboard"
                    st.rerun()
        else:
            # Regular navigation for admin users
            nav_cols = st.columns(4)
            with nav_cols[0]:
                if st.button("Dashboard", key="nav_dashboard", use_container_width=True):
                    st.session_state.navigation = "Dashboard"
                    st.rerun()
            with nav_cols[1]:
                if st.button("View Assessments", key="nav_view", use_container_width=True):
                    st.session_state.navigation = "View Assessments"
                    st.rerun()
            with nav_cols[2]:
                if st.button("Manage Links", key="nav_links", use_container_width=True):
                    st.session_state.navigation = "Manage Links"
                    st.rerun()
            with nav_cols[3]:
                if st.button("Logout", key="nav_logout", use_container_width=True):
                    st.session_state.authenticated = False
                    st.session_state.user = None
                    st.session_state.navigation = "Dashboard"
                    st.rerun()
        
        st.markdown("---")
        
        # Main content
        if st.session_state.navigation == "Dashboard":
            create_dashboard(assessment_manager)
        elif st.session_state.navigation == "New Assessment":
            create_assessment_form(assessment_manager)
        elif st.session_state.navigation == "View Assessments":
            view_assessments(assessment_manager)
        elif st.session_state.navigation == "QR Code" and st.session_state.user["role"] == "doctor":
            display_doctor_qr(st.session_state.user["id"])
        else:
            manage_assessment_links(link_manager)
            
MODERN_THEME = """
/* Import Inter font */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

/* CSS Variables */
:root {
    --primary-color: #4F46E5;
    --primary-light: #818CF8;
    --success-color: #22C55E;
    --warning-color: #F59E0B;
    --error-color: #EF4444;
    --text-color: #1F2937;
    --text-light: #6B7280;
    --background-light: #FFFFFF;
    --border-color: #E5E7EB;
}

/* Reset Streamlit's default styling */
.stApp {
    background: #FFFFFF !important;
}

/* Main text color reset */
.st-emotion-cache-183lzff {
    color: var(--text-color) !important;
}

/* Input fields and labels */
.stTextInput label, 
.stNumberInput label, 
.stSelectbox label,
.stTextInput span,
.stNumberInput span,
.stSelectbox span {
    color: var(--text-color) !important;
}

.stTextInput input,
.stNumberInput input,
.stSelectbox input {
    color: var(--text-color) !important;
    background-color: #F9FAFB !important;
}

/* Main container */
.main {
    font-family: 'Inter', sans-serif;
    color: var(--text-color);
    background-color: var(--background-light);
}

/* Forms */
div[data-testid="stForm"] {
    background-color: #FFFFFF;
    padding: 2rem;
    border-radius: 1rem;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    border: 1px solid #F3F4F6;
}

/* Headers */
h1, h2, h3, .stTitle {
    font-family: 'Inter', sans-serif;
    font-weight: 600;
    color: var(--text-color) !important;
    margin-bottom: 1.5rem;
}

/* Input fields */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stSelectbox > div > div {
    background-color: #F9FAFB;
    border: 1px solid var(--border-color);
    padding: 0.75rem;
    border-radius: 0.5rem;
    color: var(--text-color);
}

/* Radio buttons - Updated for visibility */
.stRadio > div {
    gap: 1rem;
    padding: 0.5rem;
}

.stRadio > div > div > label {
    background-color: #f8f9fa;
    padding: 0.75rem 1rem;
    border-radius: 0.5rem;
    border: 1px solid #dee2e6;
    margin-bottom: 0.5rem;
    width: 100%;
    display: flex !important;
    align-items: center !important;
}

.stRadio > div > div > label:hover {
    background-color: #e9ecef;
    cursor: pointer;
}

.stRadio > div > div > label > div {
    margin-right: 0.75rem !important;
    opacity: 1 !important;
    visibility: visible !important;
}

/* Specifically target radio button text color and visibility */
.stRadio label {
    color: var(--text-color) !important;
    opacity: 1 !important;
    visibility: visible !important;
}

.stRadio label span p {
    color: var(--text-color) !important;
    opacity: 1 !important;
    visibility: visible !important;
}

/* Fix for radio button text in different Streamlit versions */
.st-emotion-cache-1inwz65 {
    opacity: 1 !important;
    visibility: visible !important;
}

.st-emotion-cache-1inwz65 p {
    opacity: 1 !important;
    visibility: visible !important;
    color: #1F2937 !important;
}

/* Additional fixes for radio button containers */
.stRadio > div[role="radiogroup"] {
    background-color: white;
    padding: 1rem;
    border-radius: 0.5rem;
    border: 1px solid #E5E7EB;
}

/* Radio button label styles */
.stRadio > div > div > label {
    background-color: #f8f9fa;
    padding: 0.75rem 1rem;
    border-radius: 0.5rem;
    border: 1px solid #dee2e6;
    margin-bottom: 0.5rem;
    width: 100%;
    display: flex !important;
    align-items: center !important;
    color: #1F2937 !important;
}

/* Radio button hover state */
.stRadio > div > div > label:hover {
    background-color: #e9ecef;
    cursor: pointer;
}

/* Force text color for all radio button related elements */
.stRadio * {
    color: #1F2937 !important;
}

/* Additional fixes for markdown text within radio buttons */
.stMarkdown p {
    color: #1F2937 !important;
    opacity: 1 !important;
    visibility: visible !important;
}

/* Fix for any nested elements within radio buttons */
.stRadio div[data-testid="stMarkdownContainer"] p {
    color: #1F2937 !important;
    opacity: 1 !important;
    visibility: visible !important;
}

/* Ensure radio buttons themselves are visible */
.stRadio input[type="radio"] {
    opacity: 1 !important;
    visibility: visible !important;
    margin-right: 0.75rem !important;
}

/* Buttons */
.stButton > button {
    background-color: var(--primary-color);
    color: white !important;
    border-radius: 0.5rem;
    padding: 0.75rem 1.5rem;
    font-weight: 500;
    border: none;
    transition: all 0.2s;
    width: 100%;
}

.stButton > button:hover {
    background-color: #4338CA;
    transform: translateY(-1px);
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}

/* Secondary button */
.secondary-button > button {
    background-color: #9CA3AF;
}

.secondary-button > button:hover {
    background-color: #6B7280;
}

/* Progress bar */
.stProgress > div > div > div {
    background-color: var(--primary-color);
    border-radius: 0.375rem;
}

/* Error messages */
.stAlert {
    background-color: #FEE2E2;
    border: 1px solid #FCA5A5;
    padding: 1rem;
    border-radius: 0.5rem;
    margin: 1rem 0;
    color: #991B1B;
    font-weight: 500;
}

/* Success messages */
.element-container div[data-testid="stMarkdownContainer"] div.success {
    background-color: #D1FAE5;
    border: 1px solid #6EE7B7;
    padding: 1rem;
    border-radius: 0.5rem;
    margin: 1rem 0;
    color: #065F46;
    font-weight: 500;
}

/* Navigation */
.nav-container {
    background-color: white;
    border-bottom: 1px solid var(--border-color);
    padding: 1rem;
    margin-bottom: 2rem;
    display: flex;
    gap: 1rem;
    align-items: center;
    border-radius: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

/* Metrics */
.stMetric {
    background-color: white;
    padding: 1.5rem;
    border-radius: 0.5rem;
    border: 1px solid var(--border-color);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

.stMetric label {
    color: var(--text-color) !important;
}

/* Expander */
.streamlit-expander {
    background-color: white;
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    margin: 0.75rem 0;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
}

/* Auth title */
.auth-title {
    color: var(--text-color);
    font-family: 'Inter', sans-serif;
    font-size: 1.5rem;
    font-weight: 600;
    margin-bottom: 1.5rem;
    text-align: center;
}

/* Sidebar */
.css-1d391kg {
    background-color: #F9FAFB;
}

.css-1d391kg * {
    color: var(--text-color) !important;
}

/* Auth container */
.auth-container {
    background-color: #FFFFFF;
    padding: 2.5rem;
    border-radius: 1rem;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
    border: 1px solid #F3F4F6;
    max-width: 400px;
    margin: 2rem auto;
}

/* Input focus states */
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus {
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(79, 70, 229, 0.1);
}

/* Placeholder text */
::placeholder {
    color: #9CA3AF !important;
    opacity: 1;
}

/* Select box */
div[data-baseweb="select"] * {
    color: var(--text-color) !important;
}

/* Checkbox labels */
.stCheckbox label span {
    color: var(--text-color) !important;
}

/* Metric values */
.stMetric [data-testid="stMetricValue"] {
    color: var(--text-color) !important;
}

/* Button text */
.stButton button p {
    color: white !important;
}

/* Additional radio button visibility fixes */
.st-emotion-cache-1inwz65 {
    opacity: 1 !important;
    visibility: visible !important;
}

.st-emotion-cache-1inwz65 p {
    opacity: 1 !important;
    visibility: visible !important;
    color: #1F2937 !important;
}

/* Radio text specific fix */
.stMarkdown p {
    color: var(--text-color) !important;
    opacity: 1 !important;
    visibility: visible !important;
}
/* Sidebar styling */
section[data-testid="stSidebar"] {
    background-color: #FFFFFF !important;
    border-right: 1px solid #E5E7EB;
}

section[data-testid="stSidebar"] > div {
    padding: 2rem 1rem;
}

/* Sidebar text */
section[data-testid="stSidebar"] .stMarkdown {
    color: #1F2937 !important;
}

section[data-testid="stSidebar"] p {
    color: #1F2937 !important;
    font-size: 0.95rem !important;
    margin-bottom: 0.5rem !important;
}

/* Sidebar step indicators */
section[data-testid="stSidebar"] .element-container {
    margin-bottom: 0.75rem;
}

/* Success and info messages in sidebar */
section[data-testid="stSidebar"] .stSuccess p {
    color: #065F46 !important;
    background-color: #D1FAE5;
    padding: 0.5rem;
    border-radius: 0.25rem;
    margin-bottom: 0.5rem;
}

section[data-testid="stSidebar"] .stInfo p {
    color: #1D4ED8 !important;
    background-color: #DBEAFE;
    padding: 0.5rem;
    border-radius: 0.25rem;
    margin-bottom: 0.5rem;
}

/* Warning messages in sidebar */
section[data-testid="stSidebar"] .stWarning p {
    color: #92400E !important;
    background-color: #FEF3C7;
    padding: 0.5rem;
    border-radius: 0.25rem;
    margin-bottom: 0.5rem;
}

/* Logo container styles */
.logo-container {
    padding: 1rem;
    background-color: white;
    border-bottom: 1px solid #E5E7EB;
    margin-bottom: 1rem;
    text-align: center;
}
/* Global text color fixes */
.stMarkdown p,
.stMarkdown span,
.stMarkdown li,
[data-testid="stMarkdownContainer"] p,
[data-testid="stMetricLabel"] p,
[data-testid="stMetricValue"] p,
[data-testid="stMetricDelta"] p,
[data-testid="stHeader"] p,
.stSelectbox p,
.stTable p,
.stExpander p,
.stExpanderContent p,
.element-container p,
.stMetric label {
    color: #1F2937 !important;
    opacity: 1 !important;
    visibility: visible !important;
}

/* Fix for metric labels and values */
[data-testid="stMetricLabel"] > div,
[data-testid="stMetricValue"] > div {
    color: #1F2937 !important;
}

/* Fix for expander text */
.streamlit-expanderHeader {
    color: #1F2937 !important;
}

/* Fix for selectbox text */
.stSelectbox > div > div > div {
    color: #1F2937 !important;
}

/* Additional markdown fixes */
.stMarkdown {
    color: #1F2937 !important;
}

/* Dashboard specific fixes */
[data-testid="stMetricValue"] {
    color: #1F2937 !important;
}

/* Fix for headers */
h1, h2, h3, h4, h5, h6 {
    color: #1F2937 !important;
}

/* Manage links specific fixes */
.streamlit-expander .streamlit-expanderContent {
    color: #1F2937 !important;
}

/* Code block styling */
.stCodeBlock > div > div pre {
    background-color: #F8FAFC !important;
    border: 1px solid #E2E8F0 !important;
    padding: 1rem !important;
    color: #1F2937 !important;
}

code {
    color: #1F2937 !important;
    background-color: #F8FAFC !important;
    padding: 0.2rem 0.4rem !important;
    border-radius: 0.25rem !important;
}

.stMarkdown code {
    color: #1F2937 !important;
    background-color: #F8FAFC !important;
    display: block !important;
    padding: 1rem !important;
    margin: 1rem 0 !important;
    border: 1px solid #E2E8F0 !important;
    border-radius: 0.375rem !important;
    white-space: pre-wrap !important;
    font-size: 0.95rem !important;
    line-height: 1.5 !important;
}
"""
# Function to complete the assessment form code
def complete_assessment_form(assessment_manager, assessment_data, link_data=None):
    # Save assessment
    result = assessment_manager.save_assessment(assessment_data)
    
    if result:
        if link_data:
            link_manager = AssessmentLink(db)
            link_manager.mark_link_used(link_data["link_id"])

        st.success("Assessment submitted successfully!")
        display_assessment_results(
            assessment_data['phq9_assessment']['score'],
            assessment_data['phq9_assessment']['severity'],
            assessment_data['phq9_assessment']['action'],
            assessment_data['gad7_assessment']['score'],
            assessment_data['gad7_assessment']['severity']
        )
        
        
        # Reset form data and step
        st.session_state.form_data = {}
        st.session_state.assessment_step = 1
        st.session_state.page = "dashboard"
        
        return True
    else:
        st.error("Failed to save assessment")
        return False

if __name__ == "__main__":
    main()