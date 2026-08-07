from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
os.chdir(r'C:\Users\L E N O V O\Desktop\AI Agent Project\pocketbase\web-questions-creator\pb_public\creator')
print('Serving on http://localhost:3000')
ThreadingHTTPServer(('', 3000), SimpleHTTPRequestHandler).serve_forever()
