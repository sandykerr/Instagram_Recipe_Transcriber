from faster_whisper import WhisperModel, BatchedInferencePipeline
import time
from pathlib import Path
import argparse

# REEL_URL = "https://www.instagram.com/reel/DbjBsEfx3j5/?igsh=MXJkd2czcjhoaGo5OA=="  # IG rigatoni reel
BASE_DIR = Path("/home/sandy/code/Instagram_Recipe_Transcriber").resolve()
REEL_PATH = BASE_DIR / "tests/data/rigatoni_edited.mp4"
BEAM_SIZE = 5  # faster whisper beam size

def parse_args():
    parser = argparse.ArgumentParser(
        prog='FasterWhisper Transcription test',
        description='Tests functionality of FasterWhisper audio transcription.'
    )
    
    parser.add_argument('-f', '--file', required=True, help='MP4 file to transcribe from.')
    parser.parse_args()
    return args

def main():
    start = time.time()
    args = parse_args()
    
    # model_size = 'small'
    model = WhisperModel("turbo", device="cpu", compute_type="int8")
    batched_model = BatchedInferencePipeline(model=model)
    
    segments, info = batched_model.transcribe(args.file, beam_size=BEAM_SIZE)
    
    end = time.time()
    print(f"Took {end-start:.2f} seconds")
    
    for segment in segments:
        print("[%.2fs -> %.2fs] %s" % (segment.start, segment.end, segment.text))

if __name__ == "__main__":
    main()