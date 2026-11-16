"""Horizontal offset: TSPL output shift + config resolution (profile / auto / global)."""

import re

import pytest
from PIL import Image

from labeljetty.config import Config, LabelProfile
from labeljetty.printer.tspl import TSPLPrinter
from tests.conftest import FakeConnection


def make_printer(x_offset_mm: float = 0.0) -> tuple[TSPLPrinter, FakeConnection]:
    con = FakeConnection()
    printer = TSPLPrinter(
        connection=con,
        label_width_mm=57,
        label_height_mm=32,
        dpi=203,
        x_offset_mm=x_offset_mm,
    )
    return printer, con


def _bitmap_header(con: FakeConnection) -> tuple[int, int, int, int]:
    """(x, y, width_bytes, height) of the single BITMAP command sent."""
    blobs = [c for c in con.sent if isinstance(c, bytes) and c.startswith(b"BITMAP ")]
    assert len(blobs) == 1
    m = re.match(rb"BITMAP (-?\d+),(-?\d+),(\d+),(\d+),0,", blobs[0])
    return tuple(int(g) for g in m.groups())


def _sent_text(con: FakeConnection) -> str:
    return "".join(c if isinstance(c, str) else c.decode("latin-1") for c in con.sent)


# --------------------------------------------------------------------------- #
#  TSPL output
# --------------------------------------------------------------------------- #
def test_zero_offset_is_unchanged():
    printer, con = make_printer()
    printer.print_text("Hi")
    assert "SIZE 57 mm,32 mm\n" in con.sent
    x, y, width_bytes, _ = _bitmap_header(con)
    assert (x, y) == (0, 0)
    assert width_bytes * 8 >= printer.width_px


def test_positive_offset_moves_bitmap_and_widens_size():
    printer, con = make_printer(x_offset_mm=4.5)
    printer.print_text("Hi")
    # 57 + 4.5 rounded up, so the shifted content isn't clipped on the right.
    assert "SIZE 62 mm,32 mm\n" in con.sent
    x, _, _, _ = _bitmap_header(con)
    assert x == round(4.5 / 25.4 * 203) == 36


def test_negative_offset_crops_left_edge():
    printer, con = make_printer(x_offset_mm=-2)
    full, _ = make_printer()
    printer.print_text("Hi")
    full.print_text("Hi")
    assert "SIZE 57 mm,32 mm\n" in con.sent  # never shrinks the label
    x, _, width_bytes, _ = _bitmap_header(con)
    _, _, full_width_bytes, _ = _bitmap_header(full.connection)
    assert x == 0
    crop_px = round(2 / 25.4 * 203)
    assert width_bytes == (full_width_bytes * 8 - crop_px + 7) // 8


def test_negative_offset_larger_than_image_sends_blank_strip():
    printer, _ = make_printer(x_offset_mm=-500)
    img = printer._prepare_1bit(Image.new("L", (64, 10), 0))
    out, x = printer._apply_x_offset(img, 0)
    assert x == 0
    assert out.width == 8
    assert out.convert("L").getextrema() == (255, 255)  # all white


def test_offset_applies_to_native_barcode_and_text():
    printer, con = make_printer(x_offset_mm=5)
    printer.print_barcode("123", x=10, y=0)
    assert "BARCODE 50,0," in _sent_text(con)  # 10 + round(5mm @ 203dpi)=40

    printer, con = make_printer(x_offset_mm=5)
    printer.print_barcode_with_text("123", text="HI")
    assert 'TEXT 50,10,"3"' in _sent_text(con)


def test_native_x_never_negative():
    printer, _ = make_printer(x_offset_mm=-10)
    assert printer._native_x(5) == 0


def test_build_images_ignore_offset():
    """Previews show the label itself — the offset only applies on the wire."""
    shifted, _ = make_printer(x_offset_mm=7)
    plain, _ = make_printer()
    assert list(shifted.build_text_image("x").getdata()) == list(
        plain.build_text_image("x").getdata()
    )


# --------------------------------------------------------------------------- #
#  Config resolution
# --------------------------------------------------------------------------- #
def make_config(**overrides) -> Config:
    base = dict(PRINTER_USB="vid:0000:pid:0000", _env_file=None)
    base.update(overrides)
    return Config(**base)


def test_resolve_defaults_to_zero():
    assert make_config().resolve_x_offset_mm(100, 150, 203) == 0.0


def test_resolve_global_offset():
    assert make_config(LABEL_X_OFFSET_MM=2.5).resolve_x_offset_mm(100, 150) == 2.5


def test_resolve_auto_center_from_max_width():
    cfg = make_config(PRINTER_MAX_WIDTH_MM=108)
    assert cfg.resolve_x_offset_mm(100, 150) == 4.0
    assert cfg.resolve_x_offset_mm(57, 32) == 25.5
    # Wider-than-head stock never yields a negative auto offset.
    assert cfg.resolve_x_offset_mm(120, 50) == 0.0


def test_resolve_global_offset_fine_tunes_auto_center():
    cfg = make_config(PRINTER_MAX_WIDTH_MM=108, LABEL_X_OFFSET_MM=-0.5)
    assert cfg.resolve_x_offset_mm(100, 150) == 3.5


def test_resolve_profile_offset_wins():
    cfg = make_config(
        PRINTER_MAX_WIDTH_MM=108,
        LABEL_X_OFFSET_MM=1,
        LABEL_PROFILES=[
            LabelProfile(name="DHL", width_mm=100, height_mm=150, x_offset_mm=6),
            LabelProfile(name="Small", width_mm=57, height_mm=32),  # no offset
        ],
    )
    assert cfg.resolve_x_offset_mm(100, 150, 203) == 6
    # Matching profile without its own offset falls through to auto + global.
    assert cfg.resolve_x_offset_mm(57, 32, 203) == 26.5


def test_resolve_profile_dpi_must_match_when_pinned():
    cfg = make_config(
        LABEL_PROFILES=[
            LabelProfile(name="300", width_mm=100, height_mm=150, dpi=300, x_offset_mm=3),
        ],
    )
    assert cfg.resolve_x_offset_mm(100, 150, 300) == 3
    assert cfg.resolve_x_offset_mm(100, 150, 203) == 0.0


def test_max_width_must_be_positive():
    with pytest.raises(ValueError):
        make_config(PRINTER_MAX_WIDTH_MM=0)


def test_label_profiles_env_json_accepts_offset(monkeypatch):
    monkeypatch.setenv(
        "LABEL_PROFILES",
        '[{"name":"DHL","width_mm":100,"height_mm":150,"x_offset_mm":4.2}]',
    )
    cfg = Config(_env_file=None)
    assert cfg.LABEL_PROFILES[0].x_offset_mm == 4.2


# --------------------------------------------------------------------------- #
#  Worker passes the resolved offset to the printer
# --------------------------------------------------------------------------- #
def test_worker_applies_profile_offset(monkeypatch, make_job, fake_connection):
    from labeljetty.service import worker as worker_mod

    monkeypatch.setattr(worker_mod, "reload_config", lambda: worker_mod.config)
    monkeypatch.setattr(
        worker_mod.config,
        "LABEL_PROFILES",
        [LabelProfile(name="DHL", width_mm=100, height_mm=150, x_offset_mm=4)],
    )
    monkeypatch.setattr(Config, "get_printer_connection", lambda self: fake_connection)

    job = make_job(label_width_mm=100, label_height_mm=150, dpi=203)
    worker_mod.PrintService().print_job(job)

    assert job.error is None
    assert "SIZE 104 mm,150 mm\n" in fake_connection.sent
    header = next(c for c in fake_connection.sent if isinstance(c, bytes) and c.startswith(b"BITMAP "))
    assert header.startswith(b"BITMAP 32,0,")  # 4mm @ 203dpi
