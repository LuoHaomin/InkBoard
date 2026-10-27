#!/usr/bin/env python3
"""导出汉王/Bigme 电纸书自带笔记（/sdcard/notes）为 PNG + SVG。

.psl 格式（逆向）: 大端。头 8 字节 magic "NPSL0001"，offset12=页面高, offset16=页面宽,
offset20=笔画数。每笔记录: [ff000000 标记][uint32 0][uint32 点数 N]
[N × (float32 x, float32 y, float32 笔宽)]。

用法: uv run python export_notes.py [--serial S] [--out DIR] [--png-only]
"""

import argparse
import os
import re
import struct
import subprocess
import sys
import tempfile

import pygame

from main import connected_serials, resolve_adb

NOTES_REMOTE = "/sdcard/notes"
INK = (20, 20, 20)


def parse_psl(path: str):
    """返回 (页宽, 页高, [笔画[(x,y), ...]])；空/损坏返回 None。"""
    try:
        data = open(path, "rb").read()
        if data[:8] != b"NPSL0001":
            return None
        h = struct.unpack(">I", data[12:16])[0]
        w = struct.unpack(">I", data[16:20])[0]
        count = struct.unpack(">I", data[20:24])[0]
        markers = [m.start() for m in re.finditer(b"\xff\x00\x00\x00", data)]
        markers = markers[:count]
        strokes = []
        for i, pos in enumerate(markers):
            n = struct.unpack(">I", data[pos + 8:pos + 12])[0]
            pts = struct.unpack(f">{n * 3}f", data[pos + 12:pos + 12 + n * 12])
            strokes.append([(pts[j * 3], pts[j * 3 + 1]) for j in range(n)])
        if not strokes:
            return None
        return w, h, strokes
    except Exception:  # noqa: BLE001
        return None


def fmt_note_name(ts: str) -> str:
    if re.fullmatch(r"\d{14}", ts):
        return (f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} "
                f"{ts[8:10]}{ts[10:12]}{ts[12:14]}")
    return ts


def render_png(page, out_path: str):
    w, h, strokes = page
    surf = pygame.Surface((w, h))
    surf.fill((255, 255, 255))
    for s in strokes:
        if len(s) == 1:
            pygame.draw.circle(surf, INK, s[0], 2)
        else:
            pygame.draw.lines(surf, INK, False, s, 4)
    pygame.image.save(surf, out_path)


def export_svg(page, out_path: str):
    w, h, strokes = page
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'viewBox="0 0 {w} {h}">',
             '<rect width="100%" height="100%" fill="white"/>']
    for s in strokes:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in s)
        if len(s) == 1:
            x, y = s[0]
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" '
                         f'fill="rgb(20,20,20)"/>')
        else:
            parts.append(f'<polyline points="{pts}" fill="none" '
                         f'stroke="rgb(20,20,20)" stroke-width="4" '
                         f'stroke-linecap="round" stroke-linejoin="round"/>')
    parts.append("</svg>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def main():
    ap = argparse.ArgumentParser(description="导出电纸书自带笔记")
    ap.add_argument("--serial", help="adb 设备（默认取第一台已连接设备）")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/Desktop/inkboard-notes-export"), help="输出目录")
    ap.add_argument("--png-only", action="store_true", help="只导出 PNG")
    ap.add_argument("--min-stroke", type=int, default=100,
                    help="笔画数低于此值视为草稿不导出（默认 100，0=全导）")
    args = ap.parse_args()

    serials = connected_serials()
    if args.serial:
        serial = args.serial
    elif serials:
        serial = serials[0]
    else:
        print("未发现 adb 设备", file=sys.stderr)
        sys.exit(1)
    print(f"设备: {serial}")

    # 拉取整个 notes 目录到本地缓存（含 .psl 矢量页；已有缓存则跳过）
    cache = os.path.expanduser("~/.hanwang-tablet/notes-cache")
    candidates = [os.path.join(cache, "notes"), cache]
    local_notes = next(
        (d for d in candidates
         if os.path.isdir(d)
         and any(re.fullmatch(r"\d{14}", f) for f in os.listdir(d))),
        None)
    if local_notes:
        print(f"使用本地缓存: {local_notes}（删除该目录可强制重新拉取）")
    else:
        probe = subprocess.run(
            [resolve_adb(), "-s", serial, "shell", f"ls {NOTES_REMOTE} | wc -l"],
            capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or "No such file" in probe.stdout + probe.stderr:
            print(f"设备上没有 {NOTES_REMOTE}（该设备可能用别的笔记目录）",
                  file=sys.stderr)
            sys.exit(1)
        print(f"远端笔记目录存在（{probe.stdout.strip()} 条），拉取到缓存...")
        os.makedirs(cache, exist_ok=True)
        subprocess.run([resolve_adb(), "-s", serial, "pull", NOTES_REMOTE,
                        cache], check=True, capture_output=True)
        local_notes = next(
            (d for d in candidates
             if os.path.isdir(d)
             and any(re.fullmatch(r"\d{14}", f) for f in os.listdir(d))), None)
        if not local_notes:
            print("拉取后未找到笔记目录", file=sys.stderr)
            sys.exit(1)

    os.makedirs(args.out, exist_ok=True)
    exported = skipped = empty = 0
    for ts in sorted(os.listdir(local_notes)):
        note_dir = os.path.join(local_notes, ts)
        if not os.path.isdir(note_dir):
            continue
        psls = sorted(f for f in os.listdir(note_dir) if f.endswith(".psl"))
        if not psls:
            empty += 1
            continue
        pages = [(i + 1, parse_psl(os.path.join(note_dir, f)))
                 for i, f in enumerate(psls)]
        pages = [(i, p) for i, p in pages if p]
        total_strokes = sum(len(p[2]) for _, p in pages)
        if not pages or (args.min_stroke and total_strokes < args.min_stroke):
            skipped += 1
            continue
        base = fmt_note_name(ts)
        for page_no, page in pages:
            png = os.path.join(args.out, f"{base} p{page_no}.png")
            render_png(page, png)
            if not args.png_only:
                export_svg(page, os.path.join(args.out, f"{base} p{page_no}.svg"))
        exported += 1
        print(f"  {base}: {len(pages)} 页 / {total_strokes} 笔")

    print(f"\n完成: 导出 {exported} 条笔记, 跳过草稿 {skipped}, 空笔记 {empty}")
    print(f"输出目录: {args.out}")


if __name__ == "__main__":
    main()
