"""Human-readable Windows filenames and no-clobber publication."""
from __future__ import annotations
import os
from pathlib import Path
import re

from .core import BomError

DEFAULT_PATTERN = "<projectname>v<revision>bom<datetime>.ods"
DEFAULT_DATETIME = "%d.%m.%Y_%H-%M"


def bom_filename(config, environment, now):
    values = {k.casefold(): v for k, v in environment.items()}
    values["datetime"] = now.strftime(config.get("plugin", "filename_datetime_format", DEFAULT_DATETIME))
    missing = set()
    def replace(match):
        key = match.group(1).strip().casefold()
        if key not in values or (key == "projectname" and not values[key].strip()):
            missing.add(key)
            return ""
        return values[key]
    pattern = config.get("plugin", "filename_pattern", DEFAULT_PATTERN)
    name = re.sub(r"<([^<>]+)>", replace, pattern)
    if missing:
        raise BomError("The BOM filename requires variables: " + ", ".join(sorted(missing)) +
                       ". Set them in DipTrace or the INI [environment] section.")
    # Sanitize the entire result so that a project value can never become a path.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name.lower().endswith(".ods"):
        raise BomError("[plugin] filename_pattern must end with .ods")
    stem = name[:-4].rstrip(". ")
    if not stem or stem in (".", ".."):
        raise BomError("The BOM output filename is empty.")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", stem.split(".")[0], re.I):
        stem = "_" + stem
    name = stem + ".ods"
    if len(name.encode("utf-16-le")) // 2 > 240:
        raise BomError("BOM filename is too long. Shorten projectname/revision or filename_pattern.")
    return name


def publish_bom(staged, output, *, automatic=False, overwrite=False):
    """Staged file is on the destination volume. Atomic, including concurrent runs."""
    staged, output = Path(staged), Path(output)
    if overwrite:
        os.replace(staged, output)
        return output
    candidate, suffix = output, 2
    while True:
        try:
            os.link(staged, candidate)
            return candidate
        except FileExistsError as error:
            if not automatic:
                raise BomError(f"Output already exists: {output}. Choose a different output filename.") from error
            candidate = output.with_name(f"{output.stem}_{suffix}{output.suffix}")
            suffix += 1
        except OSError as error:
            # Some removable/network filesystems cannot make hard links. An
            # exclusive copy still preserves previous BOMs (it is not atomic).
            import errno
            import shutil
            if error.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            try:
                stream = candidate.open("xb")
            except FileExistsError:
                if not automatic:
                    raise BomError(f"Output already exists: {candidate}")
                candidate = output.with_name(f"{output.stem}_{suffix}{output.suffix}")
                suffix += 1
                continue
            try:
                with stream, staged.open("rb") as source:
                    shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                candidate.unlink(missing_ok=True)
                raise
            return candidate
