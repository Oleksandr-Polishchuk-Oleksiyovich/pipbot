import sys, json, os, time
sys.path.insert(0, os.path.dirname(__file__)); import rep_counter as rc
v, model, tf, ms = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
r = rc.analyze_video(v, plot=False, target_fps=tf or None if tf else 0, max_side=ms or 0, model=model, use_cache=False)
print("RESULT", json.dumps(dict(v=os.path.basename(v)[:10], model=model, fps=tf, side=ms, t=r["processing_s"], ex=r["exercise"], reps=r["reps"], peaks=r.get("peak_count"))))
