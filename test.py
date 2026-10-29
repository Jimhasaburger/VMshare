import av
from io import BytesIO
from PIL import Image
from av.video.frame import VideoFrame

w, h = 1920, 1088
output = BytesIO()
container = av.open(output, format="mp4", mode="w", options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
stream = container.add_stream("mpeg4", rate=15)
stream.width = w
stream.height = h
stream.pix_fmt = "yuv420p"

img = Image.new("RGB", (w, h))
frame = VideoFrame.from_image(img)
for packet in stream.encode(frame):
    container.mux(packet)

print("successe!", len(output.getvalue()))