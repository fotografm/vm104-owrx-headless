#!/usr/bin/env python3
"""
Re-apply WSJTX 2.x compatibility patches to OpenWebRX+ after apt upgrade.
Safe to run multiple times (idempotent).

Patches:
  wsjt.py   — Jt9Decoder.parse(): handle decoded.txt format (depth field,
              decimal freq, MODE suffix) from WSJTX 2.x jt9
  queue.py  — QueueJob.run(): read decodes from decoded.txt instead of stdout;
              truncate decoded.txt before each run so jt9's overwrite-not-append
              behaviour is handled correctly (fixes zero PSKReporter spots)
"""

import os
import sys

WSJT_PATH  = '/usr/lib/python3/dist-packages/owrx/wsjt.py'
QUEUE_PATH = '/usr/lib/python3/dist-packages/owrx/audio/queue.py'

# ── wsjt.py ───────────────────────────────────────────────────────────────────

def patch_wsjt():
    with open(WSJT_PATH) as f:
        content = f.read()
    lines = content.split('\n')

    jt9_class_idx = next((i for i, l in enumerate(lines) if 'class Jt9Decoder' in l), None)
    if jt9_class_idx is None:
        print("wsjt.py: Jt9Decoder class not found — skipping"); return False

    parse_idx = next((i for i in range(jt9_class_idx, jt9_class_idx + 50)
                      if lines[i].strip().startswith('def parse(self, msg, dial_freq)')), None)
    if parse_idx is None:
        print("wsjt.py: Jt9Decoder.parse not found — skipping"); return False

    if 'New format: depth[0:3]' in '\n'.join(lines[parse_idx:parse_idx + 35]):
        print("wsjt.py: already patched"); return False

    end_idx = parse_idx + 1
    while end_idx < len(lines):
        l = lines[end_idx]
        if (l.startswith('    def ') or l.startswith('class ')) and end_idx > parse_idx + 1:
            break
        end_idx += 1

    new_method = [
        '    def parse(self, msg, dial_freq):',
        '        # Supports two jt9 output formats:',
        '        # Old (stdout): HHMMSS db dt freq ~ message',
        '        # New (decoded.txt): HHMMSS depth db dt freq. sync message... MODE',
        '        msg, timestamp = self.parse_timestamp(msg)',
        '',
        '        try:',
        '            # Old format: freq field at [9:13] is a plain integer (no decimal)',
        '            freq_offset = int(msg[9:13])',
        '            wsjt_msg = msg[17:53].strip()',
        '            result = {',
        '                "timestamp": timestamp,',
        '                "db": float(msg[0:3]),',
        '                "dt": float(msg[4:8]),',
        '                "freq": dial_freq + freq_offset,',
        '                "msg": wsjt_msg,',
        '            }',
        '        except ValueError:',
        '            # New format: depth[0:3] db[3:8] dt[8:14] freq.[14:22] sync[22:26] msg[29:]...MODE',
        '            raw_msg_field = msg[29:].rstrip()',
        '            parts = raw_msg_field.rsplit(None, 1)',
        '            wsjt_msg = parts[0].rstrip() if len(parts) == 2 else raw_msg_field',
        '            result = {',
        '                "timestamp": timestamp,',
        '                "db": float(msg[3:8]),',
        '                "dt": float(msg[8:14]),',
        '                "freq": dial_freq + int(float(msg[14:22])),',
        '                "msg": wsjt_msg,',
        '            }',
        '',
        '        result.update(self.messageParser.parse(wsjt_msg))',
        '        return result',
        '',
    ]

    new_lines = lines[:parse_idx] + new_method + lines[end_idx:]
    with open(WSJT_PATH, 'w') as f:
        f.write('\n'.join(new_lines))
    print(f"wsjt.py: patched (replaced lines {parse_idx}–{end_idx - 1})")
    return True


# ── queue.py ──────────────────────────────────────────────────────────────────

# Original QueueJob.run() body from OpenWebRX+ 1.2.x (post-apt-upgrade state)
OLD_QUEUE_RUN = '''\
    def run(self):
        logger.debug("processing file %s", self.file)
        tmp_dir = CoreConfig().get_temporary_directory()
        decoder = subprocess.Popen(
            ["nice", "-n", "10"] + self.profile.decoder_commandline(self.file),
            stdout=subprocess.PIPE,
            cwd=tmp_dir,
            close_fds=True,
            )
        lines = None
        try:
            lines = [l for l in decoder.stdout]
        except OSError:
            decoder.stdout.flush()
            # TODO uncouple parsing from the output so that decodes can still go to the map and the spotters
            logger.debug("output has gone away while decoding job.")

        # keep this out of the try/except
        if lines is not None:
            self.writer.sendResult(QueueJobResult(self.profile, self.frequency, lines))'''

# Patched version: reads from decoded.txt; truncates it first so jt9's
# overwrite-not-append behaviour doesn't silently drop every decode.
NEW_QUEUE_RUN = '''\
    def run(self):
        logger.debug("processing file %s", self.file)
        tmp_dir = CoreConfig().get_temporary_directory()
        decoded_file = os.path.join(tmp_dir, "decoded.txt")

        # Truncate decoded.txt before jt9 runs — jt9 2.x overwrites (not appends),
        # so pre_size based on old file size always misses new content. Reset to 0.
        pre_size = 0
        try:
            open(decoded_file, 'wb').close()
        except OSError:
            pass

        decoder = subprocess.Popen(
            ["nice", "-n", "10"] + self.profile.decoder_commandline(self.file),
            stdout=subprocess.PIPE,
            cwd=tmp_dir,
            close_fds=True,
            )
        # drain stdout (jt9 2.x writes only <DecodeFinished> here; actual decodes go to decoded.txt)
        try:
            for _ in decoder.stdout:
                pass
        except OSError:
            decoder.stdout.flush()
            logger.debug("output has gone away while decoding job.")

        # keep this out of the try/except
        lines = []
        try:
            with open(decoded_file, "rb") as f:
                f.seek(pre_size)
                lines = f.readlines()
        except OSError:
            pass

        if lines:
            self.writer.sendResult(QueueJobResult(self.profile, self.frequency, lines))'''


def patch_queue():
    with open(QUEUE_PATH) as f:
        content = f.read()

    if NEW_QUEUE_RUN in content:
        print("queue.py: already patched"); return False

    if OLD_QUEUE_RUN not in content:
        print("queue.py: unexpected content — cannot patch automatically; inspect manually")
        sys.exit(1)

    content = content.replace(OLD_QUEUE_RUN, NEW_QUEUE_RUN, 1)
    with open(QUEUE_PATH, 'w') as f:
        f.write(content)
    print("queue.py: patched")
    return True


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if os.geteuid() != 0:
        print("Run as root: sudo python3 patch_owrx.py")
        sys.exit(1)

    wsjt_changed  = patch_wsjt()
    queue_changed = patch_queue()

    if wsjt_changed or queue_changed:
        print("Done — restart openwebrx: sudo systemctl restart openwebrx")
    else:
        print("All patches already in place — nothing to do")
