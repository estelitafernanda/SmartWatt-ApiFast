import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

db = None

def inicializar_firebase():
    global db
    if not firebase_admin._apps:
        cred_json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")
        
        print(f"Tamanho do JSON: {len(cred_json_str) if cred_json_str else 'VAZIO'}")
        
        if cred_json_str:
            cred_json = json.loads(cred_json_str)
            cred = credentials.Certificate(cred_json)
        else:
            cred = credentials.Certificate("serviceAccountKey.json")
        
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    return db

def get_db():
    global db
    if db is None:
        inicializar_firebase()
    return db