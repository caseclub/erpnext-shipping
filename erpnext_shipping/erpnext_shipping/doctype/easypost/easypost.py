# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt
#frappe.log_error(title="Bill_3p 2", message=f"bill_3p: {bill_3p}, clean_acct: {acct_3p}")
#/apps/erpnext_shipping/erpnext_shipping/erpnext_shipping/doctype/easypost
import json
import base64
import io
import os
import re                # strips non‑alphanumerics from account numbers
import uuid
import time
from typing import List, Dict, Any

import frappe
import requests
from PIL import Image
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_link_to_form, get_url, get_files_path
from requests.exceptions import HTTPError

from erpnext_shipping.erpnext_shipping.utils import show_error_alert

# UPS-Direct imports -----------------------------------------------------------
from .ups_direct import UPSDirect
# FedEx-Direct imports -----------------------------------------------------------
from .fedex_direct import FedExDirect
from .fedex_direct import TRANSIT_TIME_MAP, FedExDirect

EASYPOST_PROVIDER = "EasyPost"

# ─────────────────────────────────────────────
# Helper: turn each row into N identical parcel dicts
# ─────────────────────────────────────────────
def build_parcel_list(rows):
    """
    Return a flat list with ONE element per physical box.
    Each element is a dict shaped exactly like EasyPost's Parcel.
    """
    parcels = []
    for row in rows:
        qty = int(row.get("count") or 1)
        parcel = {
            "length":  row["length"],
            "width":   row["width"],
            "height":  row["height"],
            "weight":  row["weight"],
        }
        parcels.extend([parcel] * qty)
    return parcels


class EasyPost(Document):
    pass


class EasyPostUtils:
    def __init__(self):
        settings = frappe.get_single("EasyPost")
        
        if settings.use_test_environment:
            self.api_key = settings.get_password("test_key")
        else:
            self.api_key = settings.get_password("production_key")  # Replace with your production key field name
        
        self.enabled = settings.enabled
        self.label_format = settings.label_format
        self.currency = frappe.db.get_single_value("Shipping Settings", "rates_currency")

        if not self.enabled:
            link = get_link_to_form("EasyPost", "EasyPost", _("EasyPost Settings"))
            frappe.throw(_("Please enable EasyPost Integration in {0}").format(link))

    # ──────────────────────────────────────────────────────────────────────
    # Human‑friendly labels for the popup (does NOT affect the API)
    # ──────────────────────────────────────────────────────────────────────
    _DISPLAY_MAP = {
        # ---- Carrier aliases the rest of the code expects ----
        "FEDEXDEFAULT": "FedEx",
        "FEDEX":        "FedEx",
        "UPSDAP":       "UPS",
        "USPS":         "USPS",
        # ---- Service renames (add / edit as you like) ----
        "FEDEX_2_DAY":          "2‑Day",
        "FEDEX_EXPRESS_SAVER":  "Express Saver",
        "FEDEX_GROUND":         "Ground",
        "PRIORITY_OVERNIGHT":   "Priority Overnight",
        "STANDARD_OVERNIGHT":   "Standard Overnight",
        "GroundAdvantage":      "Ground Advantage",
        "3DaySelect":           "3‑Day",
        "SMART_POST":           "Smart Post",
        "2ndDayAir":            "2‑Day",
        "NextDayAirSaver":      "Next Day Air Saver",
        "NextDayAir":           "Next Day Air",
        "FEDEX_2_DAY_AM":       "2‑Day AM",
        "NextDayAirEarlyAM":    "Next Day Air AM",
        "2ndDayAirAM":          "2‑Day AM",
        "UPSGroundsaverGreaterThan1lb": "Ground Saver",
        "FIRST_OVERNIGHT": "Next Day Air AM",   
    }

    def _pretty(self, raw: str) -> str:
        """Return nicer label if we have one."""
        return self._DISPLAY_MAP.get(raw, raw)

    def _rate_in_all_shipments(self, rate_obj, shipments) -> bool:
        """True iff every shipment has a rate with the same carrier+service."""
        c = rate_obj["carrier"]
        s = rate_obj["service"]
        for sh in shipments:
            if not any(r["carrier"] == c and r["service"] == s for r in sh.get("rates", [])):
                return False
        return True

    # NEW: Fetch raw ZPL text from a URL
    def _fetch_zpl_content(self, url: str) -> str:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return r.text

    # NEW: Save ZPL content to a local .zpl file, create File doc, return full URL
    def _save_zpl_content(self, content: str) -> str:
        fname = f"{uuid.uuid4()}.zpl"
        file_path = os.path.join(get_files_path(is_private=True), fname)
        with open(file_path, "w") as f:
            f.write(content)

        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": fname,
            "file_url": f"/private/files/{fname}",
            "is_private": 1,
        }).insert(ignore_permissions=True)

        return f"{get_url()}{file_doc.file_url}"

    # NEW: Download multiple ZPLs, concatenate, save as single .zpl, return URL
    def _zpls_to_zpl(self, urls: list[str]) -> str:
        contents = [self._fetch_zpl_content(u) for u in urls if u]
        combined = "\n\n".join(contents)  # Separate labels with blank lines for safety
        return self._save_zpl_content(combined)
    
    # =========================================================================
    # MAIN RATE‑SHOPPING ENDPOINT
    # =========================================================================
    def get_available_services(self, delivery_address, delivery_contact, shipment_parcel: List[Dict[str, Any]], pickup_address, pickup_contact, value_of_goods, ) -> List[Dict[str, Any]]:
            if not self.enabled or not self.api_key:
                return []

            parcels = build_parcel_list(shipment_parcel)
            if not parcels:
                frappe.throw(_("No parcel data supplied to EasyPost."))
            parcel_count = len(parcels)
            first_parcel = parcels[0]
            mps = parcel_count > 1        # multi-piece shipment?

            parcel_block = ({"parcel": first_parcel} # 1 box only
            if parcel_count == 1 else {"parcels": parcels}) # 2+ boxes

            # ------------------------------------------------------------------
            # Third‑party billing extraction for UPS
            # ------------------------------------------------------------------
            third_party_block = {}

            ship_doc = frappe.get_doc("Shipment", shipment_parcel[0]["parent"])
            if not ship_doc:
                frappe.throw(_("Could not locate parent Shipment for delivery address"))

            # Fetch receiver details using centralized helper
            to_address = self._build_address_dict(ship_doc, delivery_contact, delivery_address, is_to_address=True)
            
            # Determine from_name: use company name if pickup_from_type is "Company", else fallback to contact name
            from_contact_full = f"{pickup_contact.first_name or ''} {pickup_contact.last_name or ''}".strip()
            from_company = None
            if ship_doc.pickup_from_type == "Company" and ship_doc.pickup_company:
                from_company = frappe.db.get_value("Company", ship_doc.pickup_company, "company_name") or "Shipping Dept"

            from_address = self._build_address_dict(ship_doc, pickup_contact, pickup_address, is_to_address=False)
            from_address["name"] = from_contact_full if not from_company else None
            from_address["company"] = from_company if from_company else from_contact_full if from_contact_full else None

            if pickup_contact.email_id:
                from_address["email"] = pickup_contact.email_id

            bill_3p = (
                ship_doc.get("custom_ship_on_third_party") in (1, "1", True, "Yes")
                and bool(ship_doc.get("custom_third_party_account"))
            )
            acct_3p     = ship_doc.custom_third_party_account          # may be None
            clean_acct  = re.sub(r"[^A-Za-z0-9]", "", acct_3p or "")
            zip_3p   = ship_doc.custom_third_party_postal
                    
            if bill_3p and len(clean_acct) == 6:          
                if not (acct_3p and zip_3p):
                    frappe.throw(_("Please fill Customer Account # and Billing ZIP for third‑party billing."))

                if len(clean_acct) == 0:
                    frappe.throw(_("Account number must contain at least one letter/number."))

                third_party_block = {
                    "payment": {
                        "type":        "THIRD_PARTY",
                        "account":     clean_acct,
                        "postal_code": zip_3p.strip(),
                        "country":     "US",
                    }
                }

            # ------------------------------------------------------------------
            # Compose EasyPost shipment payload
            # ------------------------------------------------------------------
            shipment: Dict[str, Any] = {
                "to_address": to_address,
                "from_address": from_address,
                **parcel_block,           # ← single or multi‑piece
                "options": {
                    "label_format": "ZPL",  # NEW: Request ZPL format
                    "currency": self.currency,
                    **third_party_block,
                },
            }

            if delivery_contact.email_id:
                shipment["to_address"]["email"] = delivery_contact.email_id
            if pickup_contact.email_id:
                shipment["from_address"]["email"] = pickup_contact.email_id

            try:
                if not mps:
                    ep_url = "https://api.easypost.com/v2/shipments"
                    payload = {"shipment": shipment}
                else:
                    # ── build an Order with one Shipment per parcel ──
                    ep_url = "https://api.easypost.com/v2/orders"
                    payload = {
                        "order": {
                            "to_address":   shipment["to_address"],
                            "from_address": shipment["from_address"],
                            "shipments": [
                                {"parcel": p, "options": {"label_format": "ZPL", "currency": shipment["options"]["currency"], **third_party_block}} for p in parcels
                            ],
                        }
                    }
                response = requests.post(ep_url, auth=(self.api_key, ""), json=payload)
                response_dict = response.json()

                # ---- Handle EasyPost API errors --------------------------------
                if "error" in response_dict:
                    frappe.throw(response_dict["error"]["message"], title=_("EasyPost"))

                available_services: List[Dict[str, Any]] = []
                for service in response_dict.get("rates", []):
                    available_service = self.get_service_dict(
                        service,
                        1 if mps else parcel_count,    # Order returns full price already
                        response_dict.get("id"),
                        is_order=mps
                    )
                    available_services.append(available_service)

                # Apply original parcel-count filter (now works with fixed carrier_code)
                if parcel_count > 1:
                    available_services = [s for s in available_services if (s.get("carrier_code") or "").upper() != "FEDEXDEFAULT"]
                else:
                    available_services = [s for s in available_services if (s.get("carrier_code") or "").upper() != "FEDEX"]

                # ─────────────────────────────────────────────────────────────
                # Add direct rates for non-3p multi-parcel FedEx (sender-billed)
                # ─────────────────────────────────────────────────────────────
                if parcel_count > 1 and not bill_3p:
                    fedex = FedExDirect()
                    try:
                        fedex_rates = fedex.rate(
                            fedex.fedex_shipper_num,
                            fedex.fedex_shipper_num,  # Bill to sender
                            from_address["zip"],      # Sender ZIP (not strictly needed but passed for consistency)
                            shipment["to_address"],
                            shipment["from_address"],
                            parcels
                        )

                        rated_shipments = fedex_rates.get("output", {}).get("rateReplyDetails", [])
                        if isinstance(rated_shipments, dict):
                            rated_shipments = [rated_shipments]

                        for rated in rated_shipments:
                            svc_type = rated.get("serviceType")
                            nice_name = FedExDirect.SERVICE_MAP.get(svc_type) or svc_type
                            price = flt(rated.get("ratedShipmentDetails", [{}])[0].get("totalNetCharge", 0.0))
                            if price == 0.0:
                                frappe.log_error("FedEx rate price is 0.0", f"Service: {svc_type}. Check response for missing charge data.")

                            transit_str = rated.get("operationalDetail", {}).get("transitTime")
                            delivery_days = TRANSIT_TIME_MAP.get(transit_str, None)
                            if delivery_days is None and transit_str:
                                try:
                                    delivery_days = int(re.search(r'\d+', transit_str).group())
                                except (AttributeError, ValueError):
                                    delivery_days = None

                            available_services.append(
                                frappe._dict({
                                    "service_provider": "FedEx",
                                    "carrier": self._pretty("FEDEX"),
                                    "carrier_code": "FEDEX",
                                    "service_name": nice_name,
                                    "total_price": price,
                                    "delivery_days": delivery_days,
                                    "service_id": svc_type,
                                    "shipment_id": None,
                                    "fedex_shipper_number": fedex.fedex_shipper_num,
                                    "fedex_account": fedex.fedex_shipper_num,
                                    "fedex_postal_code": from_address["zip"],
                                    "to_address": shipment["to_address"],
                                    "from_address": shipment["from_address"],
                                    "parcels": parcels,
                                })
                            )
                    except HTTPError as e:
                        print("FedEx HTTPError:", getattr(e.response, "text", str(e)), flush=True)
                        raise
                    except Exception as e:
                        frappe.throw(_(f"Unexpected error fetching FedEx rates: {str(e)}"))

                # ─────────────────────────────────────────────────────────────
                # UPSDirect integration for third‑party UPS billing
                # ─────────────────────────────────────────────────────────────
                if bill_3p:
                    if len(clean_acct) == 6:  # UPS (6-digit)
                        ups = UPSDirect()
                        try:
                            ups_rates = ups.rate(
                                ups.ups_shipper_num,
                                clean_acct,
                                zip_3p.strip(),
                                shipment["to_address"],
                                shipment["from_address"],
                                parcels
                            )

                            rated_shipments = ups_rates.get("RateResponse", {}).get("RatedShipment", [])
                            if isinstance(rated_shipments, dict):
                                rated_shipments = [rated_shipments]

                            for rated in rated_shipments:
                                svc = rated["Service"]
                                code = svc.get("Code") or ""
                                nice_name = (
                                    svc.get("Description")
                                    or UPSDirect.SERVICE_MAP.get(code)
                                    or code
                                )

                                available_services.append(
                                    frappe._dict({
                                        "service_provider": "UPS",
                                        "carrier": "UPS",
                                        "service_name": nice_name,
                                        "total_price": flt(rated["TotalCharges"]["MonetaryValue"]),
                                        "delivery_days": rated.get("GuaranteedDaysToDelivery"),
                                        "service_id": code,
                                        "shipment_id": None,
                                        "ups_shipper_number": ups.ups_shipper_num,
                                        "ups_account":        clean_acct,
                                        "ups_postal_code":    zip_3p.strip(),
                                        "to_address":         shipment["to_address"],
                                        "from_address":       shipment["from_address"],
                                        "parcels":             parcels,
                                    })
                                )
                        except HTTPError as e:
                            print("UPS HTTPError:", getattr(e.response, "text", str(e)), flush=True)
                            raise  # propagate the HTTP 4xx/5xx back to Frappe
                        except Exception as e:
                            frappe.throw(_(f"Unexpected error creating UPS label: {str(e)}"))

                        # Remove EasyPost's UPS rates to avoid duplicates (prefer 3p direct)
                        available_services = [s for s in available_services if not (s.get("service_provider") == "EasyPost" and s.get("carrier") == "UPS")]

                    elif len(clean_acct) == 9:  # FedEx (9-digit)
                        fedex = FedExDirect()
                        try:
                            fedex_rates = fedex.rate(
                                fedex.fedex_shipper_num,
                                clean_acct,
                                zip_3p.strip(),
                                shipment["to_address"],
                                shipment["from_address"],
                                parcels
                            )

                            rated_shipments = fedex_rates.get("output", {}).get("rateReplyDetails", [])
                            if isinstance(rated_shipments, dict):
                                rated_shipments = [rated_shipments]

                            for rated in rated_shipments:
                                svc_type = rated.get("serviceType")
                                nice_name = FedExDirect.SERVICE_MAP.get(svc_type) or svc_type
                                price = flt(rated.get("ratedShipmentDetails", [{}])[0].get("totalNetCharge", 0.0))
                                if price == 0.0:
                                    frappe.log_error("FedEx rate price is 0.0", f"Service: {svc_type}. Check response for missing charge data.")

                                transit_str = rated.get("operationalDetail", {}).get("transitTime")
                                delivery_days = TRANSIT_TIME_MAP.get(transit_str, None)
                                if delivery_days is None and transit_str:
                                    try:
                                        delivery_days = int(re.search(r'\d+', transit_str).group())
                                    except (AttributeError, ValueError):
                                        delivery_days = None

                                available_services.append(
                                    frappe._dict({
                                        "service_provider": "FedEx",
                                        "carrier": self._pretty("FEDEX"),
                                        "carrier_code": "FEDEX",
                                        "service_name": nice_name,
                                        "total_price": price,
                                        "delivery_days": delivery_days,
                                        "service_id": svc_type,
                                        "shipment_id": None,
                                        "fedex_shipper_number": fedex.fedex_shipper_num,
                                        "fedex_account": clean_acct,
                                        "fedex_postal_code": zip_3p.strip(),
                                        "to_address": shipment["to_address"],
                                        "from_address": shipment["from_address"],
                                        "parcels": parcels,
                                    })
                                )
                        except HTTPError as e:
                            print("FedEx HTTPError:", getattr(e.response, "text", str(e)), flush=True)
                            raise
                        except Exception as e:
                            frappe.throw(_(f"Unexpected error fetching FedEx rates: {str(e)}"))

                # ---- Filter by carrier for 3‑P billing (original logic, kept for UPS but now redundant with above)
                if bill_3p:
                    if len(clean_acct) == 6:      # UPS (6‑digit acct)
                        available_services = [s for s in available_services if s.get("service_provider") == "UPS"]
                    elif len(clean_acct) == 9:    # FedEx (9-digit)
                        available_services = [s for s in available_services if s.get("service_provider") == "FedEx"]

                return available_services

            except Exception:
                show_error_alert("fetching EasyPost prices")

    # =========================================================================
    # PURCHASE LABELS (EasyPost or UPSDirect)
    # =========================================================================
    def create_shipment(self, service_info):
        if not self.enabled or not self.api_key:
            return []

        try:
            service_info = frappe._dict(service_info)
            # ───────────────────────────
            # UPS 3rd Party Billing
            # ───────────────────────────
            if (service_info.get("service_provider") or "").upper() == "UPS":
                ups = UPSDirect()
                try:
                    resp = ups.ship(
                        service_info.ups_shipper_number,
                        service_info.ups_account,
                        service_info.ups_postal_code,
                        service_info.to_address,
                        service_info.from_address,
                        service_info.parcels,
                        service_info.service_id,
                    )

                    return {
                        "service_provider": "UPS",
                        "shipment_id": resp["shipment_id"],
                        "carrier": "UPS",
                        "carrier_service": service_info.service_name,
                        "shipment_amount": resp.get("shipment_amount", service_info.total_price),
                        "awb_number": resp["awb_number"],
                        "label_bundle": resp["label_bundle"],
                        "shipping_label": resp["shipping_label"],
                        "postage_label": {
                            "label_url": resp["shipping_label"],
                            "label_zpl_url": resp["shipping_label"],
                        },
                    }
                except HTTPError as e:
                    try:
                        err_json = e.response.json()
                        detail = (
                            err_json.get("response", {})
                                    .get("errors", [{}])[0]
                                    .get("message")
                            or err_json
                        )
                    except Exception:
                        detail = e.response.text or str(e)

                    frappe.throw(_("UPS API Error: {0}").format(detail))

            # ───────────────────────────
            # FedEx 3rd Party Billing (multiple & single parcel)
            # ───────────────────────────
            if (service_info.get("service_provider") or "").upper() == "FEDEX":
                fedex = FedExDirect()
                try:
                    resp = fedex.ship(
                        service_info.fedex_shipper_number,
                        service_info.fedex_account,
                        service_info.fedex_postal_code,
                        service_info.to_address,
                        service_info.from_address,
                        service_info.parcels,
                        service_info.service_id,
                    )

                    tracking_numbers = resp.get("awb_number", "").split(", ")
                    label_urls = resp.get("label_bundle", [])

                    if not label_urls:
                        frappe.throw(_("FedEx did not return any labels."))

                    shipping_label = label_urls[0] if label_urls[0].endswith('.zpl') else (
                        self._png_to_pdf(label_urls[0]) if len(label_urls) == 1 else self._pngs_to_pdf(label_urls)
                    )
                    
                    is_zpl = shipping_label.endswith('.zpl') if shipping_label else False

                    return {
                        "service_provider": "FedEx",
                        "shipment_id": resp["shipment_id"],
                        "carrier": "FedEx",
                        "carrier_service": service_info.service_name,
                        "shipment_amount": service_info.total_price,
                        "awb_number": resp["awb_number"],
                        "label_bundle": label_urls,
                        "shipping_label": shipping_label,
                        "postage_label": {
                            "label_url": shipping_label,
                            "label_png_url": label_urls[0] if not is_zpl else None,
                            "label_pdf_url": shipping_label if not is_zpl else None,
                            "label_zpl_url": shipping_label if is_zpl else None,
                        },
                    }
                except HTTPError as e:
                    try:
                        err_json = e.response.json()
                        detail = err_json.get("errors", [{}])[0].get("message") or err_json
                    except Exception:
                        detail = e.response.text or str(e)
                    frappe.throw(_("FedEx API Error: {0}").format(detail))

            # ───────────────────────────
            # EasyPost Multi-Parcel (Order)
            # ───────────────────────────
            if service_info.get("is_order"):
                payload = {
                    "carrier": service_info.get("carrier_code") or service_info["carrier"].lower(),
                    "service": service_info["service_code"],
                }
                response = requests.post(
                    f'https://api.easypost.com/v2/orders/{service_info["order_id"]}/buy',
                    auth=(self.api_key, ""),
                    json=payload,
                )
                response_data = response.json()
                if "error" in response_data:
                    frappe.throw(_("EasyPost Error: {0}").format(response_data["error"].get("message", "Unknown error")))

                label_urls = []
                for shp in response_data.get("shipments", []):
                    postage_label = shp.get("postage_label", {})
                    zpl_url = postage_label.get("label_zpl_url")
                    if zpl_url:
                        label_urls.append(zpl_url)
                    else:
                        png_url = postage_label.get("label_png_url") or postage_label.get("label_url")
                        if png_url:
                            label_urls.append(png_url)

                if not label_urls:
                    frappe.throw(_("EasyPost did not return any label URLs for this Order."))

                # Assume ZPL since requested at creation; combine ZPLs
                zpl_local_url = self._zpls_to_zpl(label_urls)

                tracking_codes = [shp.get("tracking_code") for shp in response_data.get("shipments", []) if shp.get("tracking_code")]

                shipment_amount = sum(flt(shp.get("selected_rate", {}).get("rate", 0.0)) for shp in response_data.get("shipments", []))

                return {
                    "service_provider": "EasyPost",
                    "shipment_id": response_data["id"],
                    "carrier": self.get_carrier(service_info["carrier"], post_or_get="post"),
                    "carrier_service": service_info["service_name"],
                    "shipment_amount": shipment_amount or service_info["total_price"],
                    "awb_number": ", ".join(tracking_codes),
                    "label_bundle": label_urls,
                    "shipping_label": zpl_local_url,
                    "postage_label": {
                        "label_url": zpl_local_url,
                        "label_zpl_url": zpl_local_url,
                    },
                }

            # ───────────────────────────
            # EasyPost Single-Parcel (Shipment)
            # ───────────────────────────
            rate = {"rate": {"id": service_info["service_id"]}}
            response = requests.post(
                f'https://api.easypost.com/v2/shipments/{service_info["shipment_id"]}/buy',
                auth=(self.api_key, ""),
                json=rate,
            )

            response_data = response.json()
            if "error" in response_data:
                frappe.throw(_("EasyPost Error: {0}").format(response_data["error"].get("message", "Unknown error")))

            if "failed_parcels" in response_data:
                error = response_data["failed_parcels"][0]["errors"]
                frappe.msgprint(_(f"Error occurred while creating Shipment: {error}"), indicator="orange", alert=True)
            else:
                postage_label = response_data.get("postage_label")
                if not postage_label:
                    frappe.throw(_("EasyPost did not return a postage_label after buying the shipment."))

                zpl_url = postage_label.get("label_zpl_url")
                if zpl_url:
                    zpl_local_url = self._zpls_to_zpl([zpl_url])
                    postage_stub = {
                        "label_url": zpl_local_url,
                        "label_zpl_url": zpl_local_url,
                    }
                    bundle = [zpl_url]
                else:
                    png_url = postage_label.get("label_png_url") or postage_label.get("label_url")
                    if not png_url:
                        frappe.throw(_("No usable label URL (ZPL or PNG) available from EasyPost."))
                    zpl_local_url = self._png_to_pdf(png_url)
                    postage_stub = {
                        "label_url": zpl_local_url,
                        "label_png_url": png_url,
                        "label_pdf_url": zpl_local_url,
                    }
                    bundle = [png_url]

                return {
                    "service_provider": "EasyPost",
                    "shipment_id": service_info["shipment_id"],
                    "carrier": self.get_carrier(service_info["carrier"], post_or_get="post"),
                    "carrier_service": service_info["service_name"],
                    "shipment_amount": service_info["total_price"],
                    "awb_number": response_data["tracker"]["tracking_code"],
                    "label_bundle": bundle,
                    "shipping_label": zpl_local_url,
                    "postage_label": postage_stub,
                }
        except Exception:
            show_error_alert("creating EasyPost Shipment")

    def get_label(self, shipment_id):
            """
            Return a printable label URL.

            • Multi-parcel Order: collect individual ZPL URLs, concatenate contents into single .zpl file.
            • Single-parcel Shipment: unchanged (saved as single .zpl).
            """
            try:
                # ── Multi-parcel Order ───────────────────────────────────────────
                if shipment_id.startswith("order_"):

                    # always fetch the fresh Order object
                    order = requests.get(
                        f"https://api.easypost.com/v2/orders/{shipment_id}",
                        auth=(self.api_key, ""),
                        timeout=20,
                    ).json()

                    # collect every individual ZPL URL
                    label_urls = [
                        s.get("postage_label", {}).get("label_url")
                        for s in order.get("shipments", [])
                        if s.get("postage_label")
                    ]
                    label_urls = [u for u in label_urls if u]

                    if not label_urls:
                        frappe.throw(_("EasyPost did not return any label URLs for this Order."))

                    # single box? concat as list of one
                    return self._zpls_to_zpl(label_urls)

                # ── Single-parcel Shipment → always return a local ZPL ─────────

                # 1️⃣  Ask EasyPost for the ZPL
                shp = requests.get(
                    f"https://api.easypost.com/v2/shipments/{shipment_id}/label",
                    auth=(self.api_key, ""),
                    params={"file_format": "zpl"},
                ).json()

                zpl_url = shp["postage_label"]["label_url"]

                # 2️⃣  Save ZPL to .zpl file (re-runs instantly if we’ve done it before)
                zpl_local_url = self._zpls_to_zpl([zpl_url])

                # 3️⃣  OPTIONAL: write back to the Shipment doctype so future
                #     clicks skip the network hop completely.
                try:
                    doc = frappe.get_doc("Shipment", {"easy_post_shipment_id": shipment_id})
                    if doc and (doc.get("shipping_label") != zpl_local_url):
                        doc.db_set("shipping_label", zpl_local_url, update_modified=False)
                except Exception:
                    # ignore if mapping field doesn’t exist or any other issue
                    pass

                return zpl_local_url


            except Exception:
                show_error_alert("printing EasyPost Label")


    def get_tracking_data(self, ep_id: str):
        """Return tracking data for a single-parcel **or** multi-parcel shipment."""
        try:
            if ep_id.startswith("order_"):                         # <-- NEW
                r = requests.get(f"https://api.easypost.com/v2/orders/{ep_id}",
                                  auth=(self.api_key, "")).json()
                
                chosen_carrier = (r.get("selected_rate") or {}).get("carrier", "").upper()
                msgs = [
                    m["message"]
                    for m in r.get("messages", [])
                    if (m.get("carrier", "") or "").upper() == chosen_carrier
                ]
                             
                # collect trackers from every parcel in the order
                tracking_codes = [
                    (sh.get("tracker") or {}).get("tracking_code") or sh.get("tracking_code")
                    for sh in r.get("shipments", [])
                ]

                return {
                    "awb_number": ", ".join(filter(None, tracking_codes)),
                    "tracking_status": r.get("status"),
                    "tracking_status_info": " / ".join(msgs) if msgs else None,
                    "tracking_url": f"https://track.easypost.com/{tracking_codes[0]}"
                                     if tracking_codes else None
                }

            # ---------- existing single-parcel logic ----------
            r = requests.get(f"https://api.easypost.com/v2/shipments/{ep_id}",
                             auth=(self.api_key, "")).json()
            t = r["tracker"]
            return {
                "awb_number": t["tracking_code"],
                "tracking_status": t["status"],
                "tracking_status_info": t["status_detail"],
                "tracking_url": t["public_url"],
            }
        except Exception:
            show_error_alert("updating EasyPost Shipment")


    def get_service_dict(self, service, multiplier, shipment_or_order_id, is_order=False):
        available_service = frappe._dict()
        available_service.service_provider = "EasyPost"
        raw_carrier = service["carrier"]
        raw_service = service["service"]

        # Fix: Distinguish EasyPost's pooled FedEx
        if raw_carrier == "FedEx":
            available_service.carrier_code = "FEDEXDEFAULT"
        else:
            available_service.carrier_code = raw_carrier.upper()

        available_service.carrier = self._pretty(available_service.carrier_code)
        available_service.service_name = self._pretty(raw_service)
        available_service.total_price = flt(service["rate"]) * multiplier
        available_service.delivery_days = service.get("delivery_days")
        available_service.service_id = service["id"]
        available_service.service_code = raw_service   # ← add
        available_service.shipment_id = None if is_order else shipment_or_order_id
        available_service.order_id    = shipment_or_order_id if is_order else None
        available_service.is_order    = is_order
        return available_service

    def get_carrier(self, carrier_name, post_or_get=None):
        if carrier_name in ("easypost", "EasyPost"):
            return "EasyPost" if post_or_get == "get" else "easypost"
        return carrier_name.upper() if post_or_get == "get" else carrier_name.lower()

    # ------------------------------------------------------------------
    # Private helper to store a base‑64 label image and return public URL
    # ------------------------------------------------------------------
    def _save_base64_png(self, data_uri: str) -> str:
        if not data_uri or not data_uri.startswith("data:image"):
            return data_uri

        header, b64 = data_uri.split(",", 1)
        ext = header.split("/")[1].split(";")[0]

        raw_bytes = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw_bytes))
        img = img.rotate(-90, expand=True)  # 90° clockwise

        fname = f"{uuid.uuid4()}.{ext}"
        file_path = os.path.join(get_files_path(is_private=True), fname)
        img.save(file_path, format=ext.upper())

        file_doc = frappe.get_doc({
            "doctype":   "File",
            "file_name": fname,
            "file_url":  f"/private/files/{fname}",
            "is_private": 1,
        })
        file_doc.insert(ignore_permissions=True)

        return f"{get_url()}{file_doc.file_url}"

    # ----------------------------------------------------------------------
    # Combine N remote PNGs into one multi-page PDF and return the File URL
    # ----------------------------------------------------------------------
    def _png_to_pdf(self, url: str) -> str:
        """Wrap single-item call so we don’t keep writing [url]."""
        return self._pngs_to_pdf([url])


    def _pngs_to_pdf(self, urls: list[str]) -> str:
        imgs: list[Image.Image] = []

        for url in urls:
            img = self._open_label_image(url)
            # im = im.rotate(-90, expand=True)
            imgs.append(img)

        if not imgs:
            frappe.throw(_("No PNGs were supplied for PDF merge."))

        fname = f"{uuid.uuid4()}.pdf"
        file_path = os.path.join(get_files_path(is_private=True), fname)

        # write a multi-page PDF – Pillow does this if save_all=True
        imgs[0].save(
            file_path,
            "PDF",
            save_all=True,
            append_images=imgs[1:]
        )

        file_doc = frappe.get_doc({
            "doctype":   "File",
            "file_name": fname,
            "file_url":  f"/private/files/{fname}",
            "is_private": 1,
        }).insert(ignore_permissions=True)

        return f"{get_url()}{file_doc.file_url}"

    # ----------------------------------------------------------------------
    # Open a label image whether it lives on S3 or in /private/files
    # ----------------------------------------------------------------------
    def _open_label_image(self, url: str) -> "Image":
        """
        • For remote (S3) links   → GET over HTTP.
        • For local /private/…    → read directly from the filesystem
          so we don’t need authentication cookies.
        """
        # Is it one of *our* private files?
        base = get_url().rstrip("/")            # e.g. https://erp.caseclub.com
        if url.startswith(f"{base}/private/files/"):
            fname = url.split("/private/files/")[1]
            file_path = os.path.join(get_files_path(is_private=True), fname)
            return Image.open(file_path).convert("RGB")

        # Otherwise download
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    
    # ─────────────────────────────────────────────
    # Helper: always return a non-blank phone string
    # ─────────────────────────────────────────────
    def _phone(self, contact, address) -> str:
        """
        FedEx's REST API will error if `phone` is missing or null.
        Priority:
          1) contact.phone
          2) address.phone  (Address DocType has an optional phone field)
          3) Company default phone (Settings → Company)
          4) hard-coded fallback so test mode never blows up
        """
        return (
            (getattr(contact, "phone", "") or "").strip()
            or (getattr(address, "phone", "") or "").strip()
            or frappe.db.get_single_value("Company", "phone_no")  # may be None
            or "714-555-0000"     # ← safe dummy; change to whatever you like
        )

    def _build_address_dict(self, ship_doc, contact, address, is_to_address=True):
        """
        Centralized logic to build address dict with proper name/company handling.
        - If delivery_customer == delivery_contact_name (or no customer): name=contact_full, company=None (individual).
        - If distinct: name=contact_full, company=customer_name (commercial with attention).
        - If company but no contact: name="Receiving Dept".
        """
        contact_full = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        company_name = None
        
        field_prefix = "delivery_" if is_to_address else "pickup_"
        customer_field = f"{field_prefix}customer"
        contact_name_field = f"{field_prefix}contact_name"
        
        if getattr(ship_doc, customer_field, None):
            customer = getattr(ship_doc, customer_field)
            contact_name = getattr(ship_doc, contact_name_field, "")
            customer_db_name = frappe.db.get_value("Customer", customer, "customer_name") or customer
            
            if customer_db_name != contact_name:
                company_name = customer_db_name
        
        if company_name and not contact_full.strip():
            contact_full = "Receiving Dept"
        
        addr_dict = {
            "name": contact_full if contact_full else None,
            "company": company_name,
            "street1": address.address_line1,
            "street2": address.address_line2,
            "city": address.city,
            "state": address.state,
            "zip": address.pincode,
            "country": address.country,
            "phone": self._phone(contact, address),
        }
        if contact.email_id:
            addr_dict["email"] = contact.email_id
        
        return addr_dict
