# Copyright The IETF Trust 2026, All Rights Reserved
import datetime
import json
from collections import defaultdict
from typing import DefaultDict, Literal

from django.core.files.storage import storages

from ietf.doc.models import Document, DocEvent
from ietf.name.models import DocTagName
from ietf.person.models import Person

ERRATA_BLOB_NAME = "other/errata.json"  # name of errata.json in the red bucket

def get_errata_last_updated() -> datetime.datetime:
    """Get timestamp of the last errata.json update
    
    May raise FileNotFoundError or other storage/S3 exceptions. Be prepared.
    """
    red_bucket = storages["red_bucket"]
    return red_bucket.get_modified_time(ERRATA_BLOB_NAME)


def get_errata_data():
    red_bucket = storages["red_bucket"]
    with red_bucket.open(ERRATA_BLOB_NAME, "r") as f:
        errata_data = json.load(f)
    return errata_data


def errata_map_from_json(errata_data):
    """Create a dict mapping RFC number to a list of applicable errata records"""
    errata = defaultdict(list)
    for item in errata_data:
        doc_id = item["doc-id"]
        if doc_id.upper().startswith("RFC"):
            rfc_number = int(doc_id[3:])
            errata[rfc_number].append(item)
    return dict(errata)


def update_errata_tags(errata_data):
    tag_has_errata = DocTagName.objects.get(slug="errata")
    tag_has_verified_errata = DocTagName.objects.get(slug="verified-errata")
    system = Person.objects.get(name="(System)")

    errata_map = errata_map_from_json(errata_data)
    nums_with_errata = [
        num
        for num, errata in errata_map.items()
        if any(er["errata_status_code"] != "Rejected" for er in errata)
    ]
    nums_with_verified_errata = [
        num
        for num, errata in errata_map.items()
        if any(er["errata_status_code"] == "Verified" for er in errata)
    ]

    rfcs_gaining_errata_tag = Document.objects.filter(
        type_id="rfc", rfc_number__in=nums_with_errata
    ).exclude(tags=tag_has_errata)

    rfcs_gaining_verified_errata_tag = Document.objects.filter(
        type_id="rfc", rfc_number__in=nums_with_verified_errata
    ).exclude(tags=tag_has_verified_errata)

    rfcs_losing_errata_tag = Document.objects.filter(
        type_id="rfc", tags=tag_has_errata
    ).exclude(rfc_number__in=nums_with_errata)

    rfcs_losing_verified_errata_tag = Document.objects.filter(
        type_id="rfc", tags=tag_has_verified_errata
    ).exclude(rfc_number__in=nums_with_verified_errata)

    # map rfc_number to add/remove lists
    changes: DefaultDict[Document, dict[str, list[DocTagName]]] = defaultdict(
        lambda: {"add": [], "remove": []}
    ) 
    for rfc in rfcs_gaining_errata_tag:
        changes[rfc]["add"].append(tag_has_errata)
    for rfc in rfcs_gaining_verified_errata_tag:    
        changes[rfc]["add"].append(tag_has_verified_errata)
    for rfc in rfcs_losing_errata_tag:
        changes[rfc]["remove"].append(tag_has_errata)
    for rfc in rfcs_losing_verified_errata_tag:
        changes[rfc]["remove"].append(tag_has_verified_errata)

    for rfc, changeset in changes.items():
        change_descs = []
        for tag in changeset["add"]:
            rfc.tags.add(tag)
            change_descs.append(f"added {tag.slug} tag")
        for tag in changeset["remove"]:
            rfc.tags.remove(tag)
            change_descs.append(f"removed {tag.slug} tag")
        summary = "Update from RFC Editor: " + ", ".join(change_descs)
        if all(
            er["errata_status_code"] == "Rejected"
            for er in errata_map[rfc.rfc_number]
        ):
            summary += " (all errata rejected)"
        DocEvent.objects.create(
            doc=rfc,
            rev=rfc.rev,  # expect no rev
            by=system,
            type="sync_from_rfc_editor",
            desc=summary
        )
