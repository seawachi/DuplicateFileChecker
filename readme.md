# Duplicate File Checker

A fast, desktop-based duplicate file finder and cleaner written in Python.

Duplicate File Checker scans a folder for identical files, verifies duplicates using **SHA-256**, and provides a graphical interface for reviewing and removing unnecessary copies. It is especially useful for cleaning up duplicate photos and videos, while still supporting any file type.

## Features

- 🔍 Scan folders recursively for duplicate files
- ⚡ Multithreaded scanning and hashing
- 🧠 Fast candidate filtering using:
  - File size
  - BLAKE2b quick fingerprints
  - Full SHA-256 verification
- 🖼️ Image previews
- 🎬 Video thumbnail previews
- 📁 Supports all file types
- 🎞️ Filter results to image/video duplicates only
- 📦 Optional scanning inside ZIP archives
- 🗑️ Recycle Bin / Trash support
- ✅ Automatically identifies the most recently modified file as the **keeper**
- ☑️ Select individual duplicate files for deletion
- 🧹 Delete all duplicates in a set while keeping the latest copy
- 🚀 Bulk-clean all duplicate sets while keeping the latest file from each set
- ⏹️ Cancel an active scan
- 📊 Scan progress and activity log

## How Duplicate Detection Works

The scanner uses several stages to avoid unnecessarily hashing every file.

### 1. File Size

Files are first grouped by size.

Files with unique sizes cannot be identical, so they are skipped.

### 2. Quick Fingerprint

Files with matching sizes receive a fast **BLAKE2b fingerprint** based on data from the beginning and end of each file.

This reduces the number of files that require full hashing.

### 3. SHA-256 Verification

Files that still match are fully hashed using **SHA-256**.

Only files with identical SHA-256 hashes are reported as true duplicates.

This gives the application the speed of a quick preliminary scan while still using full-file verification before declaring files duplicates.

## Requirements

- Python 3
- Tkinter
- Pillow *(optional, recommended for image previews)*
- OpenCV *(optional, required for video previews)*
- Send2Trash *(optional, recommended for safer deletion)*

The core duplicate scanning functionality uses Python's standard library. Optional dependencies enable additional preview and deletion features.

## Installation

Clone the repository:

```bash
git clone https://github.com/seawachi/DuplicateFileChecker.git
cd DuplicateFileChecker
```

Install the recommended dependencies:

```bash
pip install pillow opencv-python Send2Trash
```

### Tkinter

Tkinter is normally included with Python on Windows and macOS.

Some Linux distributions require it to be installed separately.

For Ubuntu/Debian:

```bash
sudo apt install python3-tk
```

## Usage

Run the application with:

```bash
python DuplicateFileChecker.py
```

Or, depending on your system:

```bash
python3 DuplicateFileChecker.py
```

## Using the Application

### 1. Select a Folder

Click **Select Folder...** and choose the directory you want to scan.

By default, **Auto-scan on select** is enabled, so scanning starts immediately after selecting a folder.

You can disable this option and use the **Scan** button manually instead.

### 2. Configure the Scan

The top toolbar provides several options:

**Threads**

Controls the number of worker threads used while fingerprinting and hashing files.

The program automatically chooses an aggressive default based on the number of CPU cores, up to 32 threads.

**Scan inside ZIPs**

Extracts ZIP archives to a temporary directory and includes their contents in duplicate detection.

The original ZIP files are not modified.

**Show only image/video sets**

Hides duplicate groups that do not contain images or videos.

### 3. Review Duplicate Sets

Detected duplicates appear in the **Duplicate Sets** panel.

Each set shows:

- Number of duplicate files
- File type
- Shortened SHA-256 hash
- Path of the keeper file

The **keeper** is automatically chosen as the file with the most recent modification time.

### 4. Preview Files

Select a duplicate set to inspect its files.

For each file, the application displays:

- File path
- Modification date
- File size
- File type
- Preview or thumbnail when available

Image previews support formats such as:

```text
JPG
JPEG
PNG
GIF
BMP
TIFF
WEBP
```

Video preview support includes formats such as:

```text
MP4
MOV
M4V
AVI
MKV
WEBM
WMV
```

Video thumbnails require OpenCV and Pillow.

## Deleting Duplicates

There are several ways to remove duplicates.

### Delete Selected Files

Check **Mark for deletion** next to the files you want to remove and click:

```text
Delete Selected
```

The keeper file cannot be selected for deletion from the normal duplicate view.

### Select All Except Latest

Click:

```text
Select All Except Latest
```

to mark every duplicate except the most recently modified copy.

### Delete a Duplicate Set

The Quick Prune option:

```text
Delete Selected Set (keep latest)
```

removes every duplicate in the currently selected set except the newest file.

### Delete Duplicates Across All Sets

The option:

```text
Delete All Sets (keep latest)
```

removes duplicate copies across every detected set while preserving the newest file in each group.

A confirmation dialog is shown before deletion.

## Recycle Bin Support

If `Send2Trash` is installed, the application enables:

```text
Use Recycle Bin
```

When enabled, deleted files are sent to your operating system's Recycle Bin or Trash instead of being permanently removed.

Install support with:

```bash
pip install Send2Trash
```

If Send2Trash is unavailable or Recycle Bin mode is disabled, files are deleted directly from the filesystem.

> **Important:** Always review the selected files before confirming deletion.

## ZIP Archive Scanning

When **Scan inside ZIPs** is enabled, ZIP archives discovered inside the selected directory are extracted into a temporary directory.

The extracted files are then included in duplicate detection.

Temporary extraction directories are cleaned up when the application exits.

ZIP scanning does **not** rewrite or remove files from the original ZIP archive.

## Performance

Duplicate File Checker is designed to reduce expensive disk operations.

Instead of calculating SHA-256 for every file immediately, it uses:

```text
All files
   ↓
Group by size
   ↓
Quick BLAKE2b fingerprint
   ↓
Possible duplicates
   ↓
Full SHA-256
   ↓
Confirmed duplicates
```

Fingerprinting and full hashing are performed using a thread pool, making the application particularly useful when scanning large directories or fast storage devices.

## Project Structure

```text
DuplicateFileChecker/
├── DuplicateFileChecker.py
└── README.md
```

The application is currently implemented as a standalone Python script.

## Safety Notes

Duplicate detection is based on full SHA-256 verification before files are placed into the same duplicate set.

However, file deletion is inherently destructive.

For safer cleanup:

1. Install `Send2Trash`.
2. Keep **Use Recycle Bin** enabled.
3. Review duplicate sets before deletion.
4. Back up important files before performing large bulk-clean operations.

The application's automatic keeper selection is based on the **most recently modified file**, which may not always be the version you personally want to preserve.

## Repository

GitHub:

```text
https://github.com/seawachi/DuplicateFileChecker
```

---

Built with Python, Tkinter, SHA-256, and a healthy dislike of duplicate files. 🧹
