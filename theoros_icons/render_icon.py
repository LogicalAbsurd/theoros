from PIL import Image, ImageDraw

CHARCOAL = (23, 29, 34, 255)   # #171D22
SLATE    = (85, 109, 124, 255) # #556D7C

def rounded_rect_mask(size, radius, ss):
    # supersampled rounded-rect alpha mask
    m = Image.new("L", (size*ss, size*ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0,0,size*ss-1,size*ss-1], radius=radius*ss, fill=255)
    return m

def render(size):
    ss = 8  # supersample factor for crisp edges
    S = size*ss
    img = Image.new("RGBA", (S, S), (0,0,0,0))
    d = ImageDraw.Draw(img)

    # field
    radius = max(2, round(size*0.21))
    d.rounded_rectangle([0,0,S-1,S-1], radius=radius*ss, fill=CHARCOAL)

    # theta proportions (relative to size)
    cx = cy = S/2
    rx = size*0.25*ss
    ry = size*0.315*ss
    sw = max(ss, round(size*0.075*ss))   # stroke width, scales, min 1px final

    # outer ellipse ring (draw filled slate ellipse, then punch charcoal interior)
    d.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], fill=SLATE)
    d.ellipse([cx-rx+sw, cy-ry+sw, cx+rx-sw, cy+ry-sw], fill=CHARCOAL)

    # crossbar
    barhalf = size*0.165*ss
    bar_t = cy - sw/2
    bar_b = cy + sw/2
    d.rounded_rectangle([cx-barhalf, bar_t, cx+barhalf, bar_b],
                        radius=sw/2, fill=SLATE)

    # downsample
    img = img.resize((size, size), Image.LANCZOS)
    img.save(f"icon-{size}.png")
    return img

for s in (16,32,48,96):
    render(s)
print("rendered:", [f"icon-{s}.png" for s in (16,32,48,96)])

# build a zoomed preview of the 96 for visual check
big = Image.open("icon-96.png").resize((288,288), Image.NEAREST)
# put on a neutral bg so transparency is visible
bg = Image.new("RGBA",(288,288),(54,69,79,255))  # the palette's slate-dark
bg.alpha_composite(big)
bg.convert("RGB").save("preview.png")
print("preview saved")
