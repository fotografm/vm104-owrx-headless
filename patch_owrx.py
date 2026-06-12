#!/usr/bin/env python3
"""
Re-apply WSJTX 2.x compatibility patches to OpenWebRX+ after apt upgrade.
Safe to run multiple times (idempotent).

Patches:
  wsjt.py   — Four fixes:
              1. WsjtParser: skip lines containing null bytes (jt9 Fortran
                 null-padding when many signals decoded at once; causes parse
                 errors and frozen FT8 display under sporadic-E conditions)
              2. Jt9Decoder.parse(): handle decoded.txt format (depth field,
                 decimal freq, MODE suffix) from WSJTX 2.x jt9
              3. Jt9Decoder.parse(): guard the new-format except block so its
                 own ValueError doesn't chain-corrupt the log
              4. WsprDecoder.parse(): use split() instead of fixed-byte slices
                 to handle both old wsprd 1.x format (7 fields) and new
                 wsprd 2.x format (8 fields with ntype prepended)

  queue.py  — QueueJob.run():
              - jt9-based decoders (FT8, FT4, JT65): read decodes from
                decoded.txt (jt9 2.x writes there, not stdout); truncate
                decoded.txt before each run to avoid concurrent-job pollution
              - wsprd (WSPR): read from stdout (wsprd writes to stdout/wspr_spots.txt,
                not decoded.txt); previous patch discarded wsprd output entirely
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

    changed = False

    # ── Part A: Jt9Decoder.parse() — new decoded.txt format ──────────────────
    jt9_class_idx = next((i for i, l in enumerate(lines) if 'class Jt9Decoder' in l), None)
    if jt9_class_idx is None:
        print("wsjt.py: Jt9Decoder class not found — skipping"); return False

    parse_idx = next((i for i in range(jt9_class_idx, jt9_class_idx + 50)
                      if lines[i].strip().startswith('def parse(self, msg, dial_freq)')), None)
    if parse_idx is None:
        print("wsjt.py: Jt9Decoder.parse not found — skipping"); return False

    if 'New format: depth[0:3]' not in '\n'.join(lines[parse_idx:parse_idx + 35]):
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
            '            try:',
            '                raw_msg_field = msg[29:].rstrip()',
            '                parts = raw_msg_field.rsplit(None, 1)',
            '                wsjt_msg = parts[0].rstrip() if len(parts) == 2 else raw_msg_field',
            '                result = {',
            '                    "timestamp": timestamp,',
            '                    "db": float(msg[3:8]),',
            '                    "dt": float(msg[8:14]),',
            '                    "freq": dial_freq + int(float(msg[14:22])),',
            '                    "msg": wsjt_msg,',
            '                }',
            '            except (ValueError, IndexError):',
            '                raise ValueError("jt9 new-format parse failed: " + repr(msg[:30])) from None',
            '',
            '        result.update(self.messageParser.parse(wsjt_msg))',
            '        return result',
            '',
        ]

        lines = lines[:parse_idx] + new_method + lines[end_idx:]
        print(f"wsjt.py: Jt9Decoder.parse patched (replaced lines {parse_idx}–{end_idx - 1})")
        changed = True
    else:
        # Already has new-format handling — check if guarded except is present
        if 'new-format parse failed' not in '\n'.join(lines[parse_idx:parse_idx + 50]):
            # Need to add the guard to the existing except block
            content_check = '\n'.join(lines)
            old_unguarded = (
                '        except ValueError:\n'
                '            # New format: depth[0:3] db[3:8] dt[8:14] freq.[14:22] sync[22:26] msg[29:]...MODE\n'
                '            raw_msg_field = msg[29:].rstrip()\n'
                '            parts = raw_msg_field.rsplit(None, 1)\n'
                '            wsjt_msg = parts[0].rstrip() if len(parts) == 2 else raw_msg_field\n'
                '            result = {\n'
                '                "timestamp": timestamp,\n'
                '                "db": float(msg[3:8]),\n'
                '                "dt": float(msg[8:14]),\n'
                '                "freq": dial_freq + int(float(msg[14:22])),\n'
                '                "msg": wsjt_msg,\n'
                '            }'
            )
            new_guarded = (
                '        except ValueError:\n'
                '            # New format: depth[0:3] db[3:8] dt[8:14] freq.[14:22] sync[22:26] msg[29:]...MODE\n'
                '            try:\n'
                '                raw_msg_field = msg[29:].rstrip()\n'
                '                parts = raw_msg_field.rsplit(None, 1)\n'
                '                wsjt_msg = parts[0].rstrip() if len(parts) == 2 else raw_msg_field\n'
                '                result = {\n'
                '                    "timestamp": timestamp,\n'
                '                    "db": float(msg[3:8]),\n'
                '                    "dt": float(msg[8:14]),\n'
                '                    "freq": dial_freq + int(float(msg[14:22])),\n'
                '                    "msg": wsjt_msg,\n'
                '                }\n'
                '            except (ValueError, IndexError):\n'
                '                raise ValueError("jt9 new-format parse failed: " + repr(msg[:30])) from None'
            )
            if old_unguarded in content_check:
                lines = content_check.replace(old_unguarded, new_guarded, 1).split('\n')
                print("wsjt.py: new-format guard added")
                changed = True
            else:
                print("wsjt.py: Jt9Decoder.parse already fully patched")
        else:
            print("wsjt.py: Jt9Decoder.parse already fully patched")

    # ── Part B: WsjtParser — skip null-byte lines ─────────────────────────────
    content_now = '\n'.join(lines)
    NULL_SKIP = 'if "\\x00" in msg:  # jt9 Fortran null-padding'
    if NULL_SKIP not in content_now:
        old_decode = (
            '            msg = raw_msg.decode().rstrip()\n'
            '            # known debug messages we know to skip\n'
            '            if msg.startswith("<DecodeFinished>"):'
        )
        new_decode = (
            '            msg = raw_msg.decode().rstrip()\n'
            '            if "\\x00" in msg:  # jt9 Fortran null-padding in decoded.txt; skip\n'
            '                return\n'
            '            # known debug messages we know to skip\n'
            '            if msg.startswith("<DecodeFinished>"):'
        )
        if old_decode not in content_now:
            print("wsjt.py: null-byte skip pattern not found — skipping")
        else:
            lines = content_now.replace(old_decode, new_decode, 1).split('\n')
            print("wsjt.py: null-byte skip added to WsjtParser")
            changed = True
    else:
        print("wsjt.py: null-byte skip already present")

    # ── Part C: WsprDecoder.parse() — split()-based parser ────────────────────
    content_now = '\n'.join(lines)

    OLD_WSPR = (
        'class WsprDecoder(Decoder):\n'
        '    def parse(self, msg, dial_freq):\n'
        '        # wspr sample\n'
        "        # '2600 -24  0.4   0.001492 -1  G8AXA JO01 33'\n"
        "        # '0052 -29  2.6   0.001486  0  G02CWT IO92 23'\n"
        '        msg, timestamp = self.parse_timestamp(msg)\n'
        '        wsjt_msg = msg[24:].strip()\n'
        '        result = {\n'
        '            "timestamp": timestamp,\n'
        '            "db": float(msg[0:3]),\n'
        '            "dt": float(msg[4:8]),\n'
        '            "freq": dial_freq + int(float(msg[10:20]) * 1e6),\n'
        '            "drift": int(msg[20:23]),\n'
        '            "msg": wsjt_msg,\n'
        '        }\n'
        '        result.update(self.messageParser.parse(wsjt_msg))\n'
        '        return result'
    )

    NEW_WSPR = (
        'class WsprDecoder(Decoder):\n'
        '    def parse(self, msg, dial_freq):\n'
        '        msg, timestamp = self.parse_timestamp(msg)\n'
        '        parts = msg.split()\n'
        '        # wsprd stdout formats (after timestamp strip):\n'
        '        # Old (7 fields):  snr  dt  freq_MHz  drift  call  loc  pwr\n'
        '        # New (8+ fields): ntype  snr  dt  freq_MHz  drift  call  loc  pwr\n'
        '        try:\n'
        '            if len(parts) >= 8:\n'
        '                snr_s, dt_s, freq_s, drift_s = parts[1], parts[2], parts[3], parts[4]\n'
        '            elif len(parts) >= 7:\n'
        '                snr_s, dt_s, freq_s, drift_s = parts[0], parts[1], parts[2], parts[3]\n'
        '            else:\n'
        '                raise ValueError("too few fields: {}".format(len(parts)))\n'
        '            wsjt_msg = \' \'.join(parts[-3:])\n'
        '            result = {\n'
        '                "timestamp": timestamp,\n'
        '                "db": float(snr_s),\n'
        '                "dt": float(dt_s),\n'
        '                "freq": dial_freq + int(float(freq_s) * 1e6),\n'
        '                "drift": int(drift_s),\n'
        '                "msg": wsjt_msg,\n'
        '            }\n'
        '        except (ValueError, IndexError):\n'
        '            raise ValueError("WSPR parse failed: " + repr(msg[:60])) from None\n'
        '        result.update(self.messageParser.parse(wsjt_msg))\n'
        '        return result'
    )

    if NEW_WSPR in content_now:
        print("wsjt.py: WsprDecoder.parse already patched")
    elif OLD_WSPR in content_now:
        lines = content_now.replace(OLD_WSPR, NEW_WSPR, 1).split('\n')
        print("wsjt.py: WsprDecoder.parse patched (split-based, handles old+new format)")
        changed = True
    else:
        print("wsjt.py: WsprDecoder.parse pattern not found — check manually")

    if changed:
        with open(WSJT_PATH, 'w') as f:
            f.write('\n'.join(lines))
    return changed


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

# Intermediate state (first patch: decoded.txt only, no wsprd split)
INTERMEDIATE_QUEUE_RUN = '''\
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

# Final patched version: jt9 decoders use decoded.txt; wsprd uses stdout.
# wsprd writes to wspr_spots.txt, not decoded.txt; and the queue has multiple
# workers, so a long wsprd job (60 s) would otherwise read FT8 content written
# to decoded.txt by a concurrent FT8 job.
NEW_QUEUE_RUN = '''\
    def run(self):
        logger.debug("processing file %s", self.file)
        tmp_dir = CoreConfig().get_temporary_directory()
        cmd = self.profile.decoder_commandline(self.file)
        is_wsprd = cmd[0] == 'wsprd'
        decoded_file = os.path.join(tmp_dir, "decoded.txt")

        if not is_wsprd:
            # jt9 2.x overwrites (not appends) decoded.txt; truncate before run
            # to avoid reading stale content from a concurrent job.
            try:
                open(decoded_file, 'wb').close()
            except OSError:
                pass

        decoder = subprocess.Popen(
            ["nice", "-n", "10"] + cmd,
            stdout=subprocess.PIPE,
            cwd=tmp_dir,
            close_fds=True,
            )

        if is_wsprd:
            # wsprd writes decodes to stdout (not decoded.txt); capture it directly.
            lines = []
            try:
                lines = [l for l in decoder.stdout]
            except OSError:
                decoder.stdout.flush()
                logger.debug("output has gone away while decoding job.")
        else:
            # jt9: drain stdout (only <DecodeFinished>); actual decodes are in decoded.txt.
            try:
                for _ in decoder.stdout:
                    pass
            except OSError:
                decoder.stdout.flush()
                logger.debug("output has gone away while decoding job.")
            lines = []
            try:
                with open(decoded_file, "rb") as f:
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

    if OLD_QUEUE_RUN in content:
        content = content.replace(OLD_QUEUE_RUN, NEW_QUEUE_RUN, 1)
        with open(QUEUE_PATH, 'w') as f:
            f.write(content)
        print("queue.py: patched (from original)")
        return True

    if INTERMEDIATE_QUEUE_RUN in content:
        content = content.replace(INTERMEDIATE_QUEUE_RUN, NEW_QUEUE_RUN, 1)
        with open(QUEUE_PATH, 'w') as f:
            f.write(content)
        print("queue.py: patched (from intermediate state — added wsprd stdout handling)")
        return True

    print("queue.py: unexpected content — cannot patch automatically; inspect manually")
    sys.exit(1)


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
