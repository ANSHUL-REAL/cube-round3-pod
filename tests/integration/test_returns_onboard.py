"""Onboarding a product for Returns: a real photo makes a placeholder card judgeable, and nothing else does.

Out of the box every organiser SKU answers `no_product_reference` (the cards have no reference photos). These tests run on a
copy of the reference folder, so the repository's cards are never touched.
"""
from __future__ import annotations

import io
import shutil

import pytest
import yaml
from PIL import Image

import agents.returns.core.refs as refs
import agents.returns.onboard as ob
from agents.returns.core.errors import MissingReference

ORG, SKU = "org_demo_alpha", "SKU-TOWEL-BLU"


def photo(color=(30, 60, 160), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture
def ref_dir(tmp_path, monkeypatch):
    src = refs.REFERENCE_DIR
    dst = tmp_path / "reference"
    shutil.copytree(src, dst)
    monkeypatch.setattr(refs, "REFERENCE_DIR", dst)
    monkeypatch.setattr(ob, "REFERENCE_DIR", dst)
    return dst


def test_out_of_the_box_the_towel_cannot_be_judged(ref_dir):
    with pytest.raises(MissingReference) as exc:
        refs.load_references(ORG, SKU)
    assert "no_product_reference" in exc.value.args[0]


def test_one_real_photo_makes_the_card_judgeable_and_resealed(ref_dir):
    before = yaml.safe_load((ref_dir / "products" / ORG / f"{SKU}.yaml").read_text())
    r = ob.onboard(ORG, SKU, photo(), view="contents_layout", actor="Anshul")
    assert r["added"] and r["image"] == "ref_contents_layout_1"
    refs.load_references(ORG, SKU)  # no MissingReference any more
    card = refs.load_card(ORG, SKU)
    assert card is not None and card.version != before["version"]
    got = refs.reference_image_bytes(card)
    assert len(got) == 1 and got[0][0] == "ref_contents_layout_1"
    after = yaml.safe_load((ref_dir / "products" / ORG / f"{SKU}.yaml").read_text())
    assert "Anshul" in after["provenance_notes"] and "SYNTHETIC placeholder" in after["provenance_notes"]  # still says what it is


def test_the_same_photo_twice_is_added_once(ref_dir):
    data = photo()
    assert ob.onboard(ORG, SKU, data)["added"]
    again = ob.onboard(ORG, SKU, data)
    assert not again["added"] and again["image"] == "ref_contents_layout_1"
    assert len(refs.load_card(ORG, SKU).reference_images) == 1


def test_a_second_view_gets_its_own_id(ref_dir):
    ob.onboard(ORG, SKU, photo((1, 2, 3)), view="front")
    ob.onboard(ORG, SKU, photo((4, 5, 6)), view="front")
    ids = [i.id for i in refs.load_card(ORG, SKU).reference_images]
    assert ids == ["ref_front_1", "ref_front_2"]


@pytest.mark.parametrize("data", [b"", b"not a picture", b"GIF89a" + b"0" * 40])
def test_only_a_readable_photo_is_accepted(ref_dir, data):
    with pytest.raises(ob.OnboardError):
        ob.onboard(ORG, SKU, data)
    with pytest.raises(MissingReference):
        refs.load_references(ORG, SKU)


@pytest.mark.parametrize("org,sku", [("../x", SKU), (ORG, "../../etc"), (ORG, "SKU/../x"), (ORG, "SKU-NOT-A-CARD")])
def test_unknown_or_unsafe_names_are_refused(ref_dir, org, sku):
    with pytest.raises(ob.OnboardError):
        ob.onboard(org, sku, photo())


def test_a_card_edited_by_hand_is_refused(ref_dir):
    p = ref_dir / "products" / ORG / f"{SKU}.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["title"] = "changed without resealing"
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(ob.OnboardError):
        ob.onboard(ORG, SKU, photo())


def test_a_reference_photo_changed_after_onboarding_stops_judgement(ref_dir):
    r = ob.onboard(ORG, SKU, photo())
    stored = next((ref_dir / "products" / ORG / SKU).iterdir())
    stored.write_bytes(photo((255, 0, 0)))  # swapped after the fact
    with pytest.raises(MissingReference):
        refs.reference_image_bytes(refs.load_card(ORG, SKU))
    assert r["added"]
