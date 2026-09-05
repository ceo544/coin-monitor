import threading, os
from app import app, init_db, loop
init_db()
threading.Thread(target=loop,daemon=True).start()
