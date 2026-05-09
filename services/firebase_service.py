import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv
import os

load_dotenv()

db = None

def inicializar_firebase():
    global db
    if not firebase_admin._apps:
        cred = credentials.Certificate(
            os.getenv("FIREBASE_CREDENTIALS", "serviceAccountKey.json")
        )
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    return db

def get_db():
    global db
    if db is None:
        inicializar_firebase()
    return db