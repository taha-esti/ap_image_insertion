"""
Reverse of the AP image extraction script.

Reads AP images from:
    AP-Images/<FloorName>/APName.png
    AP-Images/<FloorName>/APName-1.png
    AP-Images/<FloorName>/APName-2.png
(Format A: images directly in floor folders.)

For each AP on that floor:
  - Creates ONE NEW NOTE PER IMAGE (Rule A).
  - Each new note has exactly one imageId.
  - Adds the new note's ID to the AP's noteIds list.
  - Writes image data into files named: image-<uuid>
  - Adds metadata for each image into images.json

Existing notes for the AP are left untouched.

Usage:
    python insert_ap_images.py project.esx
    python insert_ap_images.py project.esx --images-dir AP-Images --inplace
"""

import argparse
import time
import zipfile
import json
import shutil
import pathlib
import os
import uuid
import re

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def extract_project(esx_file, extract_dir):
    with zipfile.ZipFile(esx_file, "r") as zf:
        zf.extractall(extract_dir)

def repack_project(extract_dir, output_esx):
    with zipfile.ZipFile(output_esx, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(extract_dir):
            for name in files:
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, extract_dir)
                zf.write(full_path, rel_path)

def parse_ap_name_from_filename(filename):
    """
    AP01.png             -> AP01
    AP01-1.png           -> AP01
    AP with spaces-2.png -> AP with spaces
    """
    base, _ = os.path.splitext(filename)
    m = re.match(r"^(.*?)(?:-\d+)?$", base)
    if m:
        return m.group(1)
    return base

def build_floor_name_to_id(floorPlans):
    mapping = {}
    for f in floorPlans.get("floorPlans", []):
        name = f.get("name")
        if name:
            mapping[name] = f.get("id")
    return mapping

def build_ap_index(accessPoints):
    """
    (floorPlanId, apName) -> AP dict
    """
    index = {}
    for ap in accessPoints.get("accessPoints", []):
        name = ap.get("name")
        loc = ap.get("location", {})
        floor_id = loc.get("floorPlanId")
        if name and floor_id:
            index[(floor_id, name)] = ap
    return index

def build_note_index(notes):
    """
    noteId -> note dict
    """
    idx = {}
    for n in notes.get("notes", []):
        nid = n.get("id")
        if nid:
            idx[nid] = n
    return idx

def collect_images(images_root, floor_name_to_id):
    """
    Return:
       { (floorPlanId, apName): [list of full image paths (sorted)] }
    Only floors that match floorPlans.json names are used.
    """
    mapping = {}
    if not os.path.isdir(images_root):
        raise FileNotFoundError(f"Images directory not found: {images_root}")

    for floor_name in os.listdir(images_root):
        floor_path = os.path.join(images_root, floor_name)
        if not os.path.isdir(floor_path):
            continue

        floor_id = floor_name_to_id.get(floor_name)
        if not floor_id:
            print(f"WARNING: Floor '{floor_name}' not found in floorPlans.json, skipping.")
            continue

        for fname in sorted(os.listdir(floor_path)):
            full = os.path.join(floor_path, fname)
            if not os.path.isfile(full):
                continue

            ext = os.path.splitext(fname)[1].lower()
            if ext not in [".png", ".jpg", ".jpeg"]:
                continue

            ap_name = parse_ap_name_from_filename(fname)
            key = (floor_id, ap_name)
            mapping.setdefault(key, []).append(full)

    return mapping

# --- images.json helpers -----------------------------------------------------

def init_images_data(images_path):
    """
    Ensure we have a dict with an "images" list in it.
    Handles:
      - existing images.json with {"images": [...]}
      - existing images.json that is just a list
      - missing file (start fresh)
    """
    if os.path.isfile(images_path):
        data = load_json(images_path)
        if "images" not in data:
            if isinstance(data, list):
                data = {"images": data}
            else:
                data["images"] = data.get("images", [])
    else:
        data = {"images": []}
    return data

def add_image_metadata(images_data, img_id, img_path):
    """
    Add a new image entry to images.json structure.

    We:
      - Clone the first existing entry as template if present.
      - Otherwise, create a minimal entry.
      - Set id, imageFormat, status.
    """
    ext = os.path.splitext(img_path)[1].lower()
    if ext.startswith("."):
        ext = ext[1:]
    image_format = ext.upper() if ext else "PNG"

    images_list = images_data.setdefault("images", [])

    if images_list:
        template = images_list[0]
        entry = json.loads(json.dumps(template))
    else:
        entry = {}

    entry["id"] = img_id
    entry["imageFormat"] = image_format
    # leave resolutionWidth/Height from template if present, otherwise omit
    entry["status"] = "CREATED"

    images_list.append(entry)

def set_note_audit_fields(note, author_name="Brett Melnychuk"):
    """
    Sets both the legacy numeric timestamps and the newer history.* fields
    so Ekahau shows the correct creator + time in the UI.
    """
    now = time.time()
    ms_since_epoch = int(now * 1000)

    # Build ISO 8601 UTC with milliseconds, e.g. "2025-11-23T18:42:10.123Z"
    seconds = int(now)
    millis = int((now - seconds) * 1000)
    iso_utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{millis:03d}Z"

    # Top-level fields
    note["created"] = ms_since_epoch
    note["modified"] = ms_since_epoch
    note["createdBy"] = author_name
    note["modifiedBy"] = author_name

    # History block (this is what the UI is clearly using)
    history = note.get("history") or {}
    history["createdAt"] = iso_utc
    history["modifiedAt"] = iso_utc
    history["createdBy"] = author_name
    history["modifiedBy"] = author_name
    note["history"] = history


# --- note creation: ONE NOTE PER IMAGE (RULE A) ------------------------------
def create_new_note_for_ap(ap, notes_data, note_index):
    """
    Create a brand new note for this AP and attach it (one image per note).
    """
    new_id = str(uuid.uuid4())

    if notes_data.get("notes"):
        template = notes_data["notes"][0]
        note = json.loads(json.dumps(template))
        note["id"] = new_id
        if "text" in note:
            note["text"] = ""
        if "title" in note:
            note["title"] = ""
        note["imageIds"] = []
    else:
        note = {
            "id": new_id,
            "text": "",
            "imageIds": [],
            "status": "CREATED"
        }

    # 🔧 Set correct timestamps + user (this fixes your issue)
    set_note_audit_fields(note, author_name="Brett Melnychuk")  # or your actual name / username

    notes_data.setdefault("notes", []).append(note)
    note_index[new_id] = note

    note_ids = ap.get("noteIds")
    if note_ids is None:
        note_ids = []
        ap["noteIds"] = note_ids
    note_ids.append(new_id)

    return note


# --- main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Insert AP note images back into an Ekahau .esx project from an "
            "AP-Images folder (Rule A: one note per image).\n\n"
            "Usage:\n"
            "  python insert_ap_images.py SRC_ESX DEST_ESX IMAGES_DIR\n"
            "Example:\n"
            "  python insert_ap_images.py project.esx project_with_images.esx AP-Images"
        )
    )
    # Positional args: source ESX, destination ESX, images directory
    parser.add_argument(
        "src_esx",
        metavar="SRC_ESX",
        help="Source Ekahau project file (.esx) to read from",
    )
    parser.add_argument(
        "dst_esx",
        metavar="DEST_ESX",
        help="Destination Ekahau project file (.esx) to write (with inserted images)",
    )
    parser.add_argument(
        "images_dir",
        metavar="IMAGES_DIR",
        help="Root directory where AP images are stored (Format A: AP-Images/<Floor>/<AP>.png)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete the extracted project folder (for debugging).",
    )

    args = parser.parse_args()

    # Paths
    esx_src_path = os.path.abspath(args.src_esx)
    output_esx = os.path.abspath(args.dst_esx)
    project_stem = pathlib.PurePath(esx_src_path).stem
    extract_dir = os.path.abspath(project_stem)
    images_root = os.path.abspath(args.images_dir)

    if not os.path.isfile(esx_src_path):
        raise FileNotFoundError(f"ESX file not found: {esx_src_path}")

    print(f"** Extracting Ekahau project: {esx_src_path}")
    if os.path.exists(extract_dir):
        print(f"   Temporary project directory '{extract_dir}' already exists, reusing it.")
    else:
        extract_project(esx_src_path, extract_dir)

    # JSON paths
    access_points_path = os.path.join(extract_dir, "accessPoints.json")
    floor_plans_path = os.path.join(extract_dir, "floorPlans.json")
    notes_path = os.path.join(extract_dir, "notes.json")
    images_json_path = os.path.join(extract_dir, "images.json")

    if not os.path.isfile(access_points_path):
        raise FileNotFoundError(f"accessPoints.json not found in {extract_dir}")
    if not os.path.isfile(floor_plans_path):
        raise FileNotFoundError(f"floorPlans.json not found in {extract_dir}")
    if not os.path.isfile(notes_path):
        raise FileNotFoundError(f"notes.json not found in {extract_dir}")

    accessPoints = load_json(access_points_path)
    floorPlans = load_json(floor_plans_path)
    notes = load_json(notes_path)
    images_data = init_images_data(images_json_path)

    floor_name_to_id = build_floor_name_to_id(floorPlans)
    ap_index = build_ap_index(accessPoints)
    note_index = build_note_index(notes)

    print(f"** Scanning images in: {images_root}")
    ap_images = collect_images(images_root, floor_name_to_id)

    if not ap_images:
        print("No AP images found to insert. Exiting.")
        return

    print(f"** Found {len(ap_images)} APs with images.")

    inserted_count = 0
    skipped_count = 0

    for (floor_id, ap_name), image_files in ap_images.items():
        ap = ap_index.get((floor_id, ap_name))
        if not ap:
            print(f"WARNING: No AP named '{ap_name}' on floor id '{floor_id}', skipping its images.")
            skipped_count += len(image_files)
            continue

        for img_path in image_files:
            # Create a brand-new note for this image
            note = create_new_note_for_ap(ap, notes, note_index)

            img_id = str(uuid.uuid4())
            note["imageIds"] = [img_id]   # exactly one image per note

            dest_filename = f"image-{img_id}"
            dest_full_path = os.path.join(extract_dir, dest_filename)

            # Write raw image file into project root
            with open(img_path, "rb") as src_f, open(dest_full_path, "wb") as dst_f:
                dst_f.write(src_f.read())

            # Add metadata entry into images.json
            add_image_metadata(images_data, img_id, img_path)

            inserted_count += 1

        print(f"   AP '{ap_name}' (floorId={floor_id}): created {len(image_files)} note(s) with images.")

    # Save modified JSON files
    save_json(access_points_path, accessPoints)
    save_json(notes_path, notes)
    save_json(images_json_path, images_data)

    # Repack into the destination ESX provided on the command line
    print(f"** Repacking project into: {output_esx}")
    repack_project(extract_dir, output_esx)

    if not args.keep_temp:
        print(f"** Cleaning up temporary directory: {extract_dir}")
        shutil.rmtree(extract_dir, ignore_errors=True)

    print(f"** Done. Inserted {inserted_count} image(s), skipped {skipped_count}.")



if __name__ == "__main__":
    start_time = time.time()
    print("** Inserting AP picture notes into Ekahau project (Rule A: one note per image)...")
    main()
    run_time = time.time() - start_time
    print("** Time to run: %s sec" % round(run_time, 2))
