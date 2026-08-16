import json
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mock_next as M

fails = []

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

# 1. _is_photo_id
check("_is_photo_id", M._is_photo_id("p5_img12") and M._is_photo_id("up_img3")
      and not M._is_photo_id("photo of a dog") and not M._is_photo_id("") and not M._is_photo_id("img12"))

# 2. fix_numeric_option_questions: listening w/ 4 descs -> picture; without -> blank; reading untouched
qs = [
    {"number": 1, "section": "listening", "options": ["1", "2", "3", "4"],
     "option_images": ["dog", "cat", "bird", "fish"], "type": "listening",
     "requiresImage": True, "imagePrompt": "x"},
    {"number": 2, "section": "listening", "options": ["1", "2", "3", "4"], "type": "listening",
     "requiresImage": True, "imagePrompt": "x", "question_text": "Q2", "explanation": "e"},
    {"number": 3, "section": "reading", "options": ["1", "2", "3", "4"], "type": "reading",
     "question_text": "Q3", "explanation": "e", "requiresImage": False},
]
nfix = M.fix_numeric_option_questions(qs)
check("fix_numeric count", nfix == 2, f"nfix={nfix}")
check("q1 -> picture", qs[0].get("picture_options") is True and qs[0].get("type") == "listening_picture"
      and qs[0].get("requiresImage") is False)
check("q2 -> blank", qs[1].get("blank") is True and qs[1].get("picture_options") is False
      and qs[1].get("options") == ["1", "2", "3", "4"] and qs[1].get("option_images") == [])
check("q3 reading untouched", qs[2].get("blank") is None and qs[2].get("picture_options") is None)

# 3. apply_blank_questions: skips picture + counts existing blanks
M.CFG = {**M.DEFAULTS, "listening_blank_count": 2}
qs2 = [
    {"number": 1, "section": "listening", "blank": True, "options": ["1", "2", "3", "4"]},
    {"number": 2, "section": "listening", "picture_options": True, "options": ["1", "2", "3", "4"],
     "option_images": ["a", "b", "c", "d"], "requiresImage": False},
    {"number": 3, "section": "listening", "options": ["A", "B", "C", "D"], "requiresImage": True, "imagePrompt": "x"},
    {"number": 4, "section": "listening", "options": ["A", "B", "C", "D"], "requiresImage": True, "imagePrompt": "x"},
]
out, nb = M.apply_blank_questions(qs2)
check("blank existing counted", nb == 2, f"nb={nb}")
check("blank skips picture", all(q.get("picture_options") for q in out if q.get("picture_options")) and sum(1 for q in out if q.get("blank")) == 2)

# 4. group_paper_pictures: >=1 imgs per question (partial grids kept), bbox sort
pdf_doc = {"images": [
    {"id": "p1_img1", "nearest_question": 5, "bbox": [0, 300, 100, 400], "png": b"1"},
    {"id": "p1_img2", "nearest_question": 5, "bbox": [0, 100, 100, 200], "png": b"2"},
    {"id": "p1_img3", "nearest_question": 5, "bbox": [0, 500, 100, 600], "png": b"3"},
    {"id": "p1_img4", "nearest_question": 5, "bbox": [0, 700, 100, 800], "png": b"4"},
    {"id": "p1_img5", "nearest_question": 5, "bbox": [0, 50, 100, 90], "png": b"5"},
    {"id": "p2_img1", "nearest_question": 6, "bbox": [0, 0, 100, 100], "png": b"6"},
    {"id": "p2_img2", "nearest_question": 6, "bbox": [0, 0, 100, 100], "png": b"7"},
]}
g = M.group_paper_pictures(pdf_doc)
check("group picks q5 and partial q6", set(g.keys()) == {5, 6}, f"keys={set(g.keys())}")
check("group top-4 by y", g[5] == ["p1_img5", "p1_img2", "p1_img1", "p1_img3"], f"order={g[5]}")
check("partial grid kept (q6 -> 2 photos)", g[6] == ["p2_img1", "p2_img2"], f"g6={g[6]}")

# 5. _paper_picture_block with stubbed vision
M.vision_caption = lambda key, png: "a dog running"
pics, caps = {}, {}
block = M._paper_picture_block(pdf_doc, "k", pics, caps)
check("block lists q5 photos", "Q5:" in block and "p1_img5" in block)
check("block flags missing photos", "MISSING" in block and "Q6:" in block, block[:160])
check("paper_pics populated", pics == {5: ["p1_img5", "p1_img2", "p1_img1", "p1_img3"], 6: ["p2_img1", "p2_img2"]}, str(pics))
check("captions populated", len(caps[5]) == 4 and all(c == "a dog running" for c in caps[5]), str(caps))

# 6. repair_picture_prompts: paper restore + demote-to-blank
M.repair_cfg = lambda: {"slug": "x", "extra": {}}
rqs = [
    {"number": 5, "section": "listening", "picture_options": True, "options": ["1", "2", "3", "4"],
     "option_images": [], "question_text": "Q5", "explanation": "e"},
    {"number": 7, "section": "listening", "picture_options": True, "options": ["1", "2", "3", "4"],
     "option_images": [], "question_text": "Q7", "explanation": "e"},
]
n = M.repair_picture_prompts("k", rqs, paper_pics={5: ["p1_img5", "p1_img2", "p1_img1", "p1_img3"]})
check("repair paper restore", n == 2 and rqs[0]["option_images"][0] == "p1_img5")
check("repair demote to blank", rqs[1].get("blank") is True)

print("\n" + ("ALL TESTS PASSED" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
