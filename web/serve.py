#!/usr/bin/env python3
"""Simple HTTP server with gzip support for pre-compressed .gz files."""
import http.server
import os

class GzipHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def do_GET(self):
        # If request is for a file and a .gz version exists, serve it with Content-Encoding
        path = self.translate_path(self.path)
        gz_path = path + '.gz'
        if os.path.isfile(gz_path) and 'gzip' in self.headers.get('Accept-Encoding', ''):
            self.path = self.path + '.gz'
            # Override send_head to add gzip headers
            f = super().do_GET()
            return f
        return super().do_GET()

    def send_head(self):
        path = self.translate_path(self.path)
        if path.endswith('.gz') and os.path.isfile(path):
            # Serve the .gz file with proper headers
            f = open(path, 'rb')
            self.send_response(200)
            # Content-Type based on the original extension
            original = path[:-3]
            if original.endswith('.json'):
                self.send_header('Content-Type', 'application/json')
            elif original.endswith('.wasm'):
                self.send_header('Content-Type', 'application/wasm')
            else:
                self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(os.fstat(f.fileno()).st_size))
            self.end_headers()
            return f
        return super().send_head()

if __name__ == '__main__':
    port = 8080
    with http.server.HTTPServer(('', port), GzipHandler) as httpd:
        print(f'Serving on http://localhost:{port}')
        httpd.serve_forever()
