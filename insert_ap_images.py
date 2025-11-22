"""
This script does the reverse of the AP image extraction script.

It will:
  - Read AP images from an "AP-Images" folder structured as:
        AP-Images/
          <FloorName>/
            AP1.png
            AP1-1.png
            AP1-2.png
            AP2.png
            ...
    (i.e., Folder format A: images directly in floor folders)

  - Open an Ekahau project file (.esx), which is a ZIP archive.
  - Extract it into a temporary directory.
  - Load:
        - accessPoints.json
        - floorPlans.json
        - notes.json
  - For each image file:
        - Match floor by folder name.
        - Match AP by file name (e.g., "AP1.png" → AP name "AP1";
          "AP1-2.png" → AP name "AP1").
        - Find the corresponding AP object on that floor.
        - Attach all images for that AP and floor to a SINGLE note
          (Rule B: one note with multiple images):
            - If AP already has at least one note:
                - Use the first note, extend its "imageIds" list.
            - If AP has no notes:
                - Clone the structure of the first existing note (if any),
                  or create a minimal new note.
                - Attach that new note to the AP.

  - Insert the image data into the project folder as files:
        image-<imageId>
    (no extension, as used by Ekahau internally)
  - Update notes.json and accessPoints.json.
  - Repack the directory into a new .esx file (backing up the original).

Usage:
    python insert_ap_images.py project.esx
    python insert_ap_images.py project.esx --images-dir AP-Images --inplace

Notes:
    - This script assumes that floor names in AP-Images/<FloorName>
      match the "name" field in floorPlans.json.
    - Image files are assumed to be PNGs, but the raw bytes are copied
      as-is; Ekahau only cares about the "image-<id>" file content,
      regardless of extension.
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
    # Create zip from folder
    with zipfile.ZipFile(output_esx, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(extract_dir):
            for name in files:
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, extract_dir)
                zf.write(full_path, rel_path)

def parse_ap_name_from_filename(filename):
    """
    From file name like:
        AP01.png        → "AP01"
        AP01-1.png      → "AP01"
        AP with spaces-2.png → "AP with spaces"
    """
    base, _ = os.path.splitext(filename)
    # Strip a trailing "-digits" part if present
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
    Build an index of (floorPlanId, apName) -> AP dict
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
    Build an index of noteId -> note dict
    """
    idx = {}
    for n in notes.get("notes", []):
        nid = n.get("id")
        if nid:
            idx[nid] = n
    return idx

def find_or_create_note_for_ap(ap, notes_data, note_index):
    """
    Ensure the AP has (at least) one note and return that note.
    Rule B: use a SINGLE note per AP and attach multiple images there.

    Strategy:
        - If AP has noteIds:
            - Use the first one and return that note.
        - Else:
            - If notes_data has at least one note: clone structure of the first.
            - Else: create a minimal new note.
            - Attach new note to AP.noteIds and add to notes_data & note_index.
    """
    # Ensure ap has noteIds list key
    note_ids = ap.get("noteIds")
    if note_ids is None:
        note_ids = []
        ap["noteIds"] = note_ids

    if note_ids:
        # Use existing first note
        first_id = note_ids[0]
        note = note_index.get(first_id)
        if note is None:
            # Weird case: id listed but note missing; create a fresh note
            new_id = str(uuid.uuid4())
            note = {"id": new_id, "imageIds": []}
            notes_data["notes"].append(note)
            note_index[new_id] = note
            note_ids[0] = new_id
        # Ensure imageIds key exists
        note.setdefault("imageIds", [])
        return note

    # No notes for this AP yet, create one
    new_id = str(uuid.uuid4())

    if notes_data.get("notes"):
        # Clone structure of first existing note as template
        template = notes_data["notes"][0]
        note = json.loads(json.dumps(template))  # deep copy
        # Overwrite ID and wipe text/content fields, but keep other structure
        note["id"] = new_id
        if "text" in note:
            note["text"] = ""
        if "title" in note:
            note["title"] = ""
        note["imageIds"] = []
    else:
        # Minimal note structure
        note = {
            "id": new_id,
            "imageIds": []
        }

    notes_data.setdefault("notes", []).append(note)
    note_index[new_id] = note
    note_ids.append(new_id)

    return note

def collect_images(images_root, floor_name_to_id):
    """
    Scan AP-Images/ folder and return a structure:
        {
            (floorPlanId, apName): [ list of image file full paths (sorted) ]
        }
    Only floors that match a floor name in floorPlans.json are used.
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

            # Only consider common image extensions (png/jpg/jpeg)
            ext = os.path.splitext(fname)[1].lower()
            if ext not in [".png", ".jpg", ".jpeg"]:
                continue

            ap_name = parse_ap_name_from_filename(fname)
            key = (floor_id, ap_name)
            mapping.setdefault(key, []).append(full)

    return mapping

def main():
    parser = argparse.ArgumentParser(
        description="Insert AP note images back into an Ekahau .esx project "
                    "from an AP-Images folder (reverse of extract script)."
    )
    parser.add_argument("file", metavar="esx_file", help="Ekahau project file (.esx)")
    parser.add_argument(
        "--images-dir",
        default="AP-Images",
        help="Root directory where AP images are stored (default: AP-Images)",
    )
    parser.add_argument(
        "--inplace",
        action="store_true",
        help="Overwrite the original .esx file (also creates a .bak backup).",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete the extracted project folder (for debugging).",
    )

    args = parser.parse_args()

    esx_path = os.path.abspath(args.file)
    project_stem = pathlib.PurePath(esx_path).stem
    extract_dir = os.path.abspath(project_stem)
    images_root = os.path.abspath(args.images_dir)

    if not os.path.isfile(esx_path):
        raise FileNotFoundError(f"ESX file not found: {esx_path}")

    print(f"** Extracting Ekahau project: {esx_path}")
    if os.path.exists(extract_dir):
        print(f"   Temporary project directory '{extract_dir}' already exists, reusing it.")
    else:
        extract_project(esx_path, extract_dir)

    # Load JSON files from extracted project
    access_points_path = os.path.join(extract_dir, "accessPoints.json")
    floor_plans_path = os.path.join(extract_dir, "floorPlans.json")
    notes_path = os.path.join(extract_dir, "notes.json")

    if not os.path.isfile(access_points_path):
        raise FileNotFoundError(f"accessPoints.json not found in {extract_dir}")
    if not os.path.isfile(floor_plans_path):
        raise FileNotFoundError(f"floorPlans.json not found in {extract_dir}")
    if not os.path.isfile(notes_path):
        raise FileNotFoundError(f"notes.json not found in {extract_dir}")

    accessPoints = load_json(access_points_path)
    floorPlans = load_json(floor_plans_path)
    notes = load_json(notes_path)

    floor_name_to_id = build_floor_name_to_id(floorPlans)
    ap_index = build_ap_index(accessPoints)
    note_index = build_note_index(notes)

    print(f"** Scanning images in: {images_root}")
    ap_images = collect_images(images_root, floor_name_to_id)

    if not ap_images:
        print("No AP images found to insert. Exiting.")
        return

    print(f"** Found {len(ap_images)} APs with images.")

    # Insert images into project
    inserted_count = 0
    skipped_count = 0

    for (floor_id, ap_name), image_files in ap_images.items():
        ap = ap_index.get((floor_id, ap_name))
        if not ap:
            # No AP with this name on that floor
            print(f"WARNING: No AP named '{ap_name}' on floor id '{floor_id}', skipping its images.")
            skipped_count += len(image_files)
            continue

        note = find_or_create_note_for_ap(ap, notes, note_index)
        note.setdefault("imageIds", [])
        image_ids = note["imageIds"]

        for img_path in image_files:
            # Generate a new image ID, create image-<id> file in project root
            img_id = str(uuid.uuid4())
            image_ids.append(img_id)

            dest_filename = f"image-{img_id}"
            dest_full_path = os.path.join(extract_dir, dest_filename)

            with open(img_path, "rb") as src_f, open(dest_full_path, "wb") as dst_f:
                dst_f.write(src_f.read())

            inserted_count += 1

        print(f"   AP '{ap_name}' (floorId={floor_id}): attached {len(image_files)} image(s).")

    # Save modified JSON files
    save_json(access_points_path, accessPoints)
    save_json(notes_path, notes)

    # Prepare output ESX path
    if args.inplace:
        backup_path = esx_path + ".bak"
        print(f"** Creating backup of original project: {backup_path}")
        shutil.copy2(esx_path, backup_path)
        output_esx = esx_path
    else:
        output_esx = os.path.splitext(esx_path)[0] + "_with_images.esx"

    print(f"** Repacking project into: {output_esx}")
    repack_project(extract_dir, output_esx)

    # Cleanup
    if not args.keep_temp:
        print(f"** Cleaning up temporary directory: {extract_dir}")
        shutil.rmtree(extract_dir, ignore_errors=True)

    print(f"** Done. Inserted {inserted_count} image(s), skipped {skipped_count}.")


if __name__ == "__main__":
    start_time = time.time()
    print("** Inserting AP picture notes into Ekahau project...")
    main()
    run_time = time.time() - start_time
    print("** Time to run: %s sec" % round(run_time, 2))
