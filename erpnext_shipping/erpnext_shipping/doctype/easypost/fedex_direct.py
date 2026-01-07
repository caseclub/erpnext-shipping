# apps/erpnext_shipping/erpnext_shipping/utils/fedex_direct.py
# (or wherever you place it; adjust imports accordingly)

import requests, json, datetime, uuid
import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password
import re
from requests.exceptions import HTTPError
import base64
import io
from PIL import Image
from frappe.utils.file_manager import get_files_path
import os

# US State Name to Code Mapping (case-insensitive lookup) - Reused from ups_direct.py
US_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
    # Territories
    "american samoa": "AS", "district of columbia": "DC", "guam": "GU",
    "northern mariana islands": "MP", "puerto rico": "PR", "virgin islands": "VI"
}

# NEW: Mapping for transitTime strings to integers
TRANSIT_TIME_MAP = {
    "ONE_DAY": 1,
    "TWO_DAYS": 2,
    "THREE_DAYS": 3,
    "FOUR_DAYS": 4,
    "FIVE_DAYS": 5,
    "SIX_DAYS": 6,
    "SEVEN_DAYS": 7,
    "EIGHT_DAYS": 8,
    "NINE_DAYS": 9,
    "TEN_DAYS": 10,
    # Add more if FedEx uses other formats
}

def _get_easypost_key():
    """
    Load EasyPost API key from settings (matches logic in easypost.py).
    """
    docname = "EasyPost"
    use_test = frappe.db.get_single_value(docname, "use_test_environment")
    fieldname = "test_key" if use_test else "production_key"
    return get_decrypted_password(docname, docname, fieldname, raise_exception=False)

def _get_fedex_creds():
    """
    Load FedEx creds from the EasyPost singleton at runtime.
    """
    docname = "EasyPost"
    
    use_test = frappe.db.get_single_value(docname, "use_test_environment")
    #use_test = True  # Force sandbox for testing
    
    if use_test:
        api_key = frappe.db.get_single_value(docname, "custom_fedex_test_api_key")
        secret = frappe.db.get_single_value(docname, "custom_fedex_test_secret_key")
        shipper = frappe.db.get_single_value(docname, "custom_fedex_test_shipper_number")
    else:
        api_key = frappe.db.get_single_value(docname, "custom_fedex_api_key")
        secret = get_decrypted_password(docname, docname, "custom_fedex_secret_key", raise_exception=False)
        shipper = frappe.db.get_single_value(docname, "custom_fedex_shipper_number")

    # UPDATED: Explicit checks for missing fields with logging and throws
    if not api_key:
        frappe.log_error("FedEx API key missing", "Check EasyPost doctype for custom_fedex_(test_)api_key")
        frappe.throw("FedEx API key is missing in EasyPost settings.")
    if not secret:
        frappe.log_error("FedEx secret key missing", "Check EasyPost doctype for custom_fedex_(test_)secret_key")
        frappe.throw("FedEx secret key is missing in EasyPost settings.")
    if not shipper:
        frappe.log_error("FedEx shipper number missing", "Check EasyPost doctype for custom_fedex_(test_)shipper_number")
        frappe.throw("FedEx shipper number is missing in EasyPost settings.")

    return api_key, secret, shipper, use_test

# Endpoints (test vs prod)
def _get_fedex_base_url(use_test):
    return "https://apis-sandbox.fedex.com" if use_test else "https://apis.fedex.com"

class FedExDirect:
    def __init__(self):
        self.fedex_api_key, self.fedex_secret, self.fedex_shipper_num, use_test = _get_fedex_creds()
        self.easypost_key = _get_easypost_key()
        self._base_url = _get_fedex_base_url(use_test)
        self.token = self._oauth()
    
    SERVICE_MAP = {
        # Domestic services
        "FEDEX_GROUND": "Ground",
        "GROUND_HOME_DELIVERY": "Home Delivery",
        "SMART_POST": "SmartPost",

        "FEDEX_EXPRESS_SAVER": "3-Day",
        "FEDEX_2_DAY": "2-Day",
        "FEDEX_2_DAY_AM": "2-Day AM",

        "STANDARD_OVERNIGHT": "Standard Overnight",
        "FEDEX_STANDARD_OVERNIGHT_EXTRA_HOURS": "Standard Overnight (Extra Hours)",

        "PRIORITY_OVERNIGHT": "Priority Overnight",
        "FEDEX_PRIORITY_OVERNIGHT_EXTRA_HOURS": "Priority Overnight (Extra Hours)",

        "FIRST_OVERNIGHT": "First Overnight",
        "FEDEX_FIRST_OVERNIGHT_EXTRA_HOURS": "First Overnight (Extra Hours)",

        # International services
        "INTERNATIONAL_ECONOMY": "International Economy",
        "INTERNATIONAL_PRIORITY": "International Priority",
    }

    
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-locale": "en_US",
        }

    # ---------- auth ----------
    def _oauth(self):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.fedex_api_key,
            "client_secret": self.fedex_secret,
        }

        r = requests.post(
            f"{self._base_url}/oauth/token",
            headers=headers,
            data=payload
        )
        r.raise_for_status()
        return r.json()["access_token"]

    # ---------- build helpers ----------
    def _address(self, d):
        # Normalize state: try as code first, then lookup by name
        raw_state = (d.get("state") or "").strip().upper()
        if len(raw_state) == 2:  # Already a code? Use as-is
            state_code = raw_state
        else:
            state_key = (d.get("state") or "").strip().lower()
            state_code = US_STATE_CODES.get(state_key)
            if not state_code:
                raise ValueError(f"Invalid state: {d['state']}. Must be 2-letter code or full name.")
        
        street_lines = [line for line in [d.get("street1", ""), d.get("street2", "")] if line]  # Filter None/empty
        if not street_lines:
            raise ValueError("At least one street line required.")
        return {
            "address": {
                "streetLines": street_lines,
                "city": d["city"],
                "stateOrProvinceCode": state_code,
                "postalCode": d["zip"],
                "countryCode": "US",
            },
            "contact": {
                "personName": d.get("name") or d.get("company") or "Unknown",
                "companyName": d.get("company") or d.get("name") or "Unknown",
                "phoneNumber": self._phone(d.get("phone")),
                "emailAddress": d.get("email"),
            }
        }

    def _phone(self, raw: str | None) -> str:
        """
        Return cleaned phone (10-15 digits, no punctuation) or empty string.
        UPDATED: Validate >=10 digits; throw if invalid to prevent FedEx API errors.
        """
        if not raw:
            return ""
        num = re.sub(r"\D", "", raw)[:15]
        if len(num) < 10:
            frappe.throw(f"Invalid phone number '{raw}': Must have at least 10 digits for FedEx.")
        return num

    def _package(self, parcel):
        """
        Build a requestedShipment/shipments/package block.
        """
        return {
            "weight": {
                "units": "LB",
                "value": parcel["weight"] / 16.0  # oz -> lb
            },
            "dimensions": {
                "length": parcel["length"],
                "width": parcel["width"],
                "height": parcel["height"],
                "units": "IN"
            },
            "packagingType": "YOUR_PACKAGING"  # Equivalent to UPS '02'
        }

    def _validate_zip(self, zip_code: str):
        """
        NEW: Validate ZIP is 5-digit numeric US ZIP.
        """
        cleaned = zip_code.strip()
        if not (len(cleaned) == 5 and cleaned.isdigit()):
            frappe.throw(f"Invalid 3P billing ZIP '{zip_code}': Must be a 5-digit numeric US ZIP code.")

    # ---------- rate ----------
    def rate(self, shipper_num, bill_acct, bill_zip, to_addr, from_addr, parcels):
        # Remove 3P logic: Always rate as SENDER with authenticating account for estimates
        # (3P applied only in ship; rates for 3P are private/unavailable)
        body = {
            "accountNumber": {"value": shipper_num},
            "rateRequestControlParameters": {  # NEW: Request transit times in response
                "returnTransitTimes": True
            },
            "requestedShipment": {
                "shipper": self._address(from_addr),
                "recipient": self._address(to_addr),
                "pickupType": "CONTACT_FEDEX_TO_SCHEDULE",
                "rateRequestType": ["LIST"],  # Use list rates for 3P estimates; change to ["ACCOUNT", "LIST"] if needed
                "requestedPackageLineItems": [self._package(p) for p in parcels],
            }
        }

        # Always use SENDER for rate requests
        body["requestedShipment"]["shippingChargesPayment"] = {
            "paymentType": "SENDER",
            "payor": {
                "responsibleParty": {
                    "accountNumber": {"value": shipper_num}
                }
            }
        }

        r = requests.post(
            f"{self._base_url}/rate/v1/rates/quotes",
            json=body,
            headers=self._headers(),
            timeout=30
        )

        if r.status_code >= 400:
            body_text = r.text.strip() or "(empty)"
            frappe.log_error(f"FedEx {r.status_code} response", body_text)
            try:
                err = r.json()
            except ValueError:
                err = body_text
            raise HTTPError(f"FedEx error {r.status_code}: {err}", response=r)

        return r.json()
    
    # In the calling code (easypost.py get_available_services), the rate response is processed like this:
    # But since you process it there, I've ensured the response is clean. No change needed here for parsing;
    # the parsing fixes are in easypost.py (see below).

    # ---------- ship / buy ----------
    def ship(self, shipper_num, bill_acct, bill_zip, to_addr, from_addr, parcels, service_code):
        today = datetime.date.today().strftime("%Y-%m-%d")
        
        is_third_party = bool(bill_acct) and (bill_acct != shipper_num)

        if is_third_party:
            if not bill_zip:
                frappe.throw("3P billing ZIP is required for FedEx third-party billing.")
            self._validate_zip(bill_zip)
            if not (isinstance(bill_acct, str) and len(bill_acct) == 9 and bill_acct.isdigit()):
                frappe.throw("Invalid 3P billing account: Must be a 9-digit numeric FedEx account number.")
        
        responsible_acct = bill_acct if is_third_party else shipper_num
        payer_block = {
            "paymentType": "THIRD_PARTY" if is_third_party else "SENDER",
            "payor": {
                "responsibleParty": {
                    "accountNumber": {"value": responsible_acct}
                }
            }
        }

        body = {
            "accountNumber": {"value": shipper_num},
            "labelResponseOptions": "LABEL",
            "requestedShipment": {
                "shipper": self._address(from_addr),
                "recipients": [self._address(to_addr)],
                "shipDateStamp": today,
                "pickupType": "USE_SCHEDULED_PICKUP",
                "serviceType": service_code,
                "packagingType": "YOUR_PACKAGING",
                "shippingChargesPayment": payer_block,
                "labelSpecification": {
                    "labelFormatType": "COMMON2D",
                    "imageType": "ZPLII",
                    "labelStockType": "STOCK_4X6"
                },
                "requestedPackageLineItems": [self._package(p) for p in parcels]
            }
        }

        if is_third_party:
            body["requestedShipment"]["shippingChargesPayment"]["payor"]["responsibleParty"]["address"] = {
                "postalCode": bill_zip,
                "countryCode": "US"
            }

        frappe.log_error("FedEx Ship Request Body", json.dumps(body, indent=2)[:20000])

        r = requests.post(f"{self._base_url}/ship/v1/shipments", json=body, headers=self._headers())
        if r.status_code >= 400:
            frappe.log_error(
                title=f"FedEx {r.status_code} body",
                message=r.text[:2000] or "(empty)"
            )
            r.raise_for_status()
        
        resp = r.json()
        frappe.log_error("FedEx Ship Full Response", json.dumps(resp, indent=2)[:2000])
        
        transaction_shipments = resp.get("output", {}).get("transactionShipments", [])
        if not transaction_shipments:
            frappe.throw("FedEx response contains no transaction shipments.")

        shipment_details = transaction_shipments[0]
        output_ship_docs = shipment_details.get("pieceResponses", [])
        if not isinstance(output_ship_docs, list):
            output_ship_docs = [output_ship_docs] if output_ship_docs else []

        if not output_ship_docs:
            frappe.throw("FedEx response contains no piece responses for labels/tracking.")

        tracking_numbers = [doc.get("trackingNumber") for doc in output_ship_docs if doc.get("trackingNumber")]
        label_contents = []  # CHANGED: Collect contents instead of URLs
        piece_responses = shipment_details.get("pieceResponses", [])
        if not piece_responses:
            piece_responses = [shipment_details]  # Fallback for single-piece

        for piece in piece_responses:
            docs = piece.get("packageDocuments", []) or piece.get("shipmentDocuments", [])
            for doc in docs:
                if doc.get("contentType") == "LABEL" and doc.get("docType") in ["ZPL", "ZPLII"]:
                    encoded_content = doc.get("encodedLabel") or doc.get("encodedLabelContent")
                    if encoded_content:
                        try:
                            label_bytes = base64.b64decode(encoded_content)
                            label_content = label_bytes.decode('utf-8')
                            label_contents.append(label_content)
                        except Exception as e:
                            frappe.log_error("FedEx ZPL Processing Error", str(e))
                            frappe.throw(_("Failed to process FedEx ZPL label: {0}").format(str(e)))

        if not label_contents:
            frappe.throw(_("FedEx did not return any label content."))

        # CHANGED: Concatenate all ZPL contents for multi-label (sequential commands work for printing)
        combined_content = ''.join(label_contents)
        local_url = self._save_label_content(combined_content, 'zpl')
        label_urls = [local_url]

        shipment_id = shipment_details.get("masterTrackingNumber") or (tracking_numbers[0] if tracking_numbers else None)
        if not shipment_id:
            frappe.log_error("FedEx ship response missing shipment ID/tracking", json.dumps(resp)[:2000])
            frappe.throw("FedEx response missing valid shipment ID or tracking number.")

        shipment_amount = float(shipment_details.get("shipmentRating", {}).get("shipmentRateDetails", [{}])[0].get("totalNetCharge", 0.0))
        
        # 3rd Party freight returns 0.0 for the dollar amount
        #if shipment_amount == 0.0:
        #    frappe.log_error("FedEx ship amount is 0.0", "Possible missing charge data in response; using fallback 0.0")

        awb_number = ", ".join(tracking_numbers) if tracking_numbers else ""

        shipping_label = local_url  # CHANGED: Single URL

        return {
            "service_provider": "FedEx",
            "shipment_id": shipment_id,
            "carrier": "FedEx",
            "carrier_service": self.SERVICE_MAP.get(service_code, service_code),
            "shipment_amount": shipment_amount,
            "awb_number": awb_number,
            "label_bundle": label_urls,  # Single URL
            "shipping_label": shipping_label,
        }

    # ---------- track ----------
    def get_tracking_data(self, shipment_id: str):
        """
        Fetch tracking data for a FedEx shipment using EasyPost's tracker API (workaround for 3p auth issues).
        Returns a dict compatible with shipping.py's update_tracking.
        """
        if not self.easypost_key:
            raise frappe.ValidationError("EasyPost API key missing in settings (required for FedEx tracking).")

        payload = {
            "tracker": {
                "tracking_code": shipment_id,
                "carrier": "FedEx"
            }
        }

        r = requests.post(
            "https://api.easypost.com/v2/trackers",
            auth=(self.easypost_key, ""),
            json=payload,
            timeout=30
        )

        # If duplicate tracker (409), EasyPost still returns the existing tracker in the response
        if r.status_code not in (200, 201, 409):
            frappe.log_error(
                title="EasyPost FedEx Tracking Error",
                message=r.text[:2000] or "(empty)"
            )
            r.raise_for_status()

        t = r.json()

        return {
            "awb_number": t.get("tracking_code", shipment_id),
            "tracking_status": t.get("status", "Unknown"),
            "tracking_status_info": t.get("status_detail", "No details available"),
            "tracking_url": t.get("public_url")
        }
    
    # NEW: General helper to save label content as file (replaces _save_base64_png)
    def _save_label_content(self, content: str, ext: str) -> str:
        fname = f"{uuid.uuid4()}.{ext}"
        file_path = os.path.join(get_files_path(is_private=True), fname)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)

        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": fname,
            "file_url": f"/private/files/{fname}",
            "is_private": 1,
        })
        file_doc.insert(ignore_permissions=True)

        return f"{frappe.utils.get_url()}{file_doc.file_url}"