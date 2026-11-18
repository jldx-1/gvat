"""Generate flat XML samples and one shared class list for browser checks."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw

from gvat_local.cvat_xml import CvatDocument
from video_fixtures import make_video

root = Path(sys.argv[1])
(root / "videos").mkdir(parents=True)
(root / "images").mkdir()
(root / "labels.txt").write_text("cat\ndog\n")
frames = []
video_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 3
for number in range(1, video_frames + 1):
    image = Image.new("RGB", (800, 600), "#e6e9e2")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 400, 800, 600), fill="#a4b8a0")
    draw.rounded_rectangle((240, 180, 560, 420), 20, fill="#cb8c5b", outline="#503a2d", width=4)
    draw.ellipse((300, 100, 500, 280), fill="#cb8c5b", outline="#503a2d", width=4)
    draw.polygon(((310, 150), (310, 70), (365, 120)), fill="#cb8c5b", outline="#503a2d")
    draw.polygon(((490, 150), (490, 70), (435, 120)), fill="#cb8c5b", outline="#503a2d")
    draw.text((30, 30), f"GVAT example {number}", fill="#263b31")
    frames.append(image)
make_video(root / "videos/source.mp4", frames)
frames[0].save(root / "images/image_01.tiff")
frames[1].save(root / "images/sequence.gif", save_all=True, append_images=[frames[2]], duration=500, loop=0)
for kind in ("images", "videos"):
    document = ET.fromstring(CvatDocument.create(["cat", "dog"], (800, 600, video_frames) if kind == "videos" else None, image_count=3))
    for label in document.findall("meta/task/labels/label"):
        attribute = ET.SubElement(label.find("attributes"), "attribute")
        for name, value in (("name", "pose"), ("mutable", "False"), ("input_type", "select"), ("default_value", "standing"), ("values", "standing\nrunning")):
            ET.SubElement(attribute, name).text = value
    if kind == "videos":
        parent = ET.SubElement(document, "track", {"id": "7", "label": "cat", "source": "auto", "group_id": "7"})
    else:
        parent = ET.SubElement(document, "image", {"id": "0", "name": "image_01.tiff", "width": "800", "height": "600"})
    box = ET.SubElement(parent, "box", {"xtl": "240", "ytl": "180", "xbr": "560", "ybr": "420", "occluded": "1", "group_id": "7"})
    if kind == "videos":
        box.attrib.update({"frame": "0", "outside": "0", "keyframe": "1"})
        ET.SubElement(parent, "box", {**box.attrib, "frame": "1", "outside": "1"})
    else:
        box.set("label", "cat")
    ET.SubElement(box, "attribute", {"name": "pose"}).text = "standing"
    (root / kind / "annotations.xml").write_bytes(ET.tostring(document, encoding="utf-8", xml_declaration=True))
