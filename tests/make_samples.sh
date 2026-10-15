#!/bin/sh
# Generate a small sample teacher folder tree (needs ffmpeg + python3).
set -eu
D=${1:?target dir}
mkdir -p "$D/Science/Video" "$D/Science/Reading" "$D/Music" "$D/.hidden"
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x240:rate=25 -f lavfi -i sine=frequency=440 -t 3 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$D/Science/Video/Lesson_1.mp4"
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x240:rate=25 -t 2 -c:v mpeg4 "$D/Science/Video/Old_codec.mp4"
ffmpeg -loglevel error -y -f lavfi -i sine=frequency=330 -t 3 -c:a libmp3lame "$D/Music/Song.mp3"
ffmpeg -loglevel error -y -f lavfi -i testsrc=size=200x150 -frames:v 1 "$D/Science/Diagram.png"
echo "notes" > "$D/Music/notes.docx"
echo x > "$D/.hidden/secret.pdf"
mkdir -p "$D/Empty"
python3 - "$D" <<'PY'
import sys, zipfile
d = sys.argv[1]
objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        None, b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
s = b"BT /F1 18 Tf 20 100 Td (IDEA sample worksheet) Tj ET"
objs[3] = b"<</Length %d>>stream\n" % len(s) + s + b"\nendstream"
out, offs = b"%PDF-1.4\n", []
for i, o in enumerate(objs, 1):
    offs.append(len(out)); out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
x = len(out)
out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
open(d + "/Science/Reading/Worksheet.pdf", "wb").write(out)
z = zipfile.ZipFile(d + "/Science/Reading/Story.epub", "w")
z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">idea-sample</dc:identifier><dc:title>Story</dc:title><dc:language>en</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="c1"/></spine></package>')
z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="c1.xhtml">One</a></li></ol></nav></body></html>')
z.writestr("OEBPS/c1.xhtml", '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>Once upon a time.</p></body></html>')
z.close()
PY
