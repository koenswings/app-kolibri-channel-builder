"""Minimal Nextcloud WebDAV client (stdlib only): auth check + folder download."""
import base64
import os
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DAV = "{DAV:}"


class AuthError(Exception):
    pass


class Nextcloud(object):
    def __init__(self, base_url, user, password, verify_tls=True):
        self.base = base_url.rstrip("/")
        self.user = user
        tok = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.headers = {"Authorization": "Basic " + tok}
        self.ctx = None if verify_tls else ssl._create_unverified_context()
        self.root = "/remote.php/dav/files/%s" % urllib.parse.quote(user)

    def _req(self, method, path, depth=None, body=None):
        h = dict(self.headers)
        if depth is not None:
            h["Depth"] = str(depth)
        if body:
            h["Content-Type"] = "application/xml"
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            return urllib.request.urlopen(r, context=self.ctx, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError("Nextcloud rejected the login")
            raise

    def _propfind(self, path, depth):
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                b'<d:prop><d:resourcetype/><d:getcontentlength/><oc:display-name/></d:prop></d:propfind>')
        with self._req("PROPFIND", path, depth, body) as resp:
            tree = ET.fromstring(resp.read())
        out = []
        for r in tree.findall(DAV + "response"):
            href = urllib.parse.unquote(r.find(DAV + "href").text)
            is_dir = r.find(".//" + DAV + "collection") is not None
            out.append((href, is_dir))
        return out

    def display_name(self):
        """Check credentials; return display name (falls back to user id)."""
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                b'<d:prop><oc:owner-display-name/></d:prop></d:propfind>')
        with self._req("PROPFIND", self.root + "/", 0, body) as resp:
            tree = ET.fromstring(resp.read())
        el = tree.find(".//{http://owncloud.org/ns}owner-display-name")
        return (el.text if el is not None and el.text else self.user)

    def download(self, folder, dest, max_bytes=20 << 30):
        """Recursively copy a user's folder into dest. Returns file count."""
        folder = "/" + folder.strip("/")
        base = self.root + urllib.parse.quote(folder)
        prefix = urllib.parse.unquote(urllib.parse.urlparse(self.base + base).path).rstrip("/") + "/"
        todo, n, total = [base + "/"], 0, 0
        while todo:
            cur = todo.pop()
            here = urllib.parse.unquote(urllib.parse.urlparse(self.base + cur).path).rstrip("/")
            for href, is_dir in self._propfind(cur, 1):
                if href.rstrip("/") == here or not href.startswith(prefix):
                    continue
                rel = href[len(prefix):].strip("/")
                if not rel or ".." in rel.split("/"):
                    continue
                target = os.path.join(dest, rel)
                if is_dir:
                    os.makedirs(target, exist_ok=True)
                    todo.append(urllib.parse.quote(href.rstrip("/")) + "/")
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with self._req("GET", urllib.parse.quote(href)) as resp, open(target, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("folder too large")
                        f.write(chunk)
                n += 1
        return n
