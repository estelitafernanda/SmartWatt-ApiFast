import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import base64

db = None

def inicializar_firebase():
    global db
    if not firebase_admin._apps:
        cred_b64 = os.getenv("FIREBASE_CREDENTIALS_BASE64")
        
        if cred_b64:
            cred_json = json.loads(base64.b64decode(cred_b64).decode("utf-8"))
            cred = credentials.Certificate(cred_json)
        else:
            # Fallback para desenvolvimento local
            cred = credentials.Certificate("serviceAccountKey.json")
        
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    return db

def get_db():
    global db
    if db is None:
        inicializar_firebase()
    return db