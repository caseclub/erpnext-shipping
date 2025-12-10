# Copyright (c) 2020, Frappe Technologies and contributors
# For license information, please see license.txt
import frappe
from frappe import _
from frappe.utils.data import get_link_to_form
from erpnext.stock.doctype.shipment.shipment import get_company_contact



def get_tracking_url(carrier, tracking_number):
    # Return the formatted Tracking URL.
    tracking_url = ""
    url_reference = frappe.get_value("Parcel Service", carrier, "url_reference")
    if url_reference:
        tracking_url = frappe.render_template(url_reference, {"tracking_number": tracking_number})
    return tracking_url


def get_address(address_name):
    address = frappe.db.get_value(
        "Address",
        address_name,
        [
            "address_title",
            "address_line1",
            "address_line2",
            "city",
            "pincode",
            "country",
            "state"
        ],
        as_dict=1,
    )
    validate_address(address)

    address.country = address.country.strip()
    address.country_code = get_country_code(address.country)
    address.pincode = address.pincode.replace(" ", "")
    address.city = address.city.strip()

    return address


def validate_address(address):
    if not address.country:
        frappe.throw(f"Please add a valid country in Address {address.address_title}.")

    if not address.pincode or address.pincode.strip() == "":
        frappe.throw(_("Please add a valid pincode in Address {0}.").format(address.address_title))


def get_country_code(country_name):
    country_code = frappe.db.get_value("Country", country_name, "code")
    if not country_code:
        frappe.throw(_("Country Code not found for {0}").format(country_name))
    return country_code


def get_contact(contact_name):
    fields = ["first_name", "last_name", "email_id", "phone", "mobile_no", "gender"]
    contact = frappe.db.get_value("Contact", contact_name, fields, as_dict=1) if contact_name else None
    
    if contact is None:
        # Fallback to company contact with user=None
        contact = get_company_contact(user=None)
    
    return normalize_contact(contact)

# After the existing get_contact function, add:

def normalize_contact(contact):
    if contact is None:
        # Build generic fallback using default Company
        default_company = frappe.defaults.get_user_default("Company")
        company_doc = frappe.get_cached_doc("Company", default_company) if default_company else None
        fallback = {
            "first_name": "Receiving",
            "last_name": "Dept",
            "email_id": company_doc.email if company_doc else "",
            "phone": company_doc.phone_no if company_doc else "",
            "mobile_no": "",
            "gender": "",
        }
        frappe.log_error("Contact fallback used", "normalize_contact created generic contact")
        return fallback
    
    # Normalize existing dict
    if not contact.get("first_name"):
        contact["first_name"] = "Receiving"
    if not contact.get("last_name"):
        contact["last_name"] = "Dept"
    if not contact.get("phone") and contact.get("mobile_no"):
        contact["phone"] = contact["mobile_no"]
    if not contact.get("email_id") or not contact.get("phone"):
        default_company = frappe.defaults.get_user_default("Company")
        if default_company:
            company_doc = frappe.get_cached_doc("Company", default_company)
            if not contact.get("email_id"):
                contact["email_id"] = company_doc.email or ""
            if not contact.get("phone"):
                contact["phone"] = company_doc.phone_no or ""
    return contact


def match_parcel_service_type_carrier(
    shipment_prices: list[dict], carrier_fieldname: str, service_fieldname: str
):
    from erpnext_shipping.erpnext_shipping.doctype.parcel_service_type.parcel_service_type import (
        match_parcel_service_type_alias,
    )

    for idx, prices in enumerate(shipment_prices):
        service_name = match_parcel_service_type_alias(
            prices.get(carrier_fieldname), prices.get(service_fieldname)
        )
        is_preferred = frappe.db.get_value(
            "Parcel Service Type", service_name, "show_in_preferred_services_list"
        )
        if is_preferred:
            shipment_prices[idx].is_preferred = is_preferred

    return shipment_prices


def show_error_alert(action):
    log = frappe.log_error(title="Shipping Error")
    link_to_log = get_link_to_form("Error Log", log.name, "See what happened.")
    frappe.msgprint(
        msg=_("An Error occurred while {0}. {1}").format(action, link_to_log), indicator="orange", alert=True
    )


def update_tracking_info_daily():
    """Daily scheduled event to update Tracking info for not delivered Shipments

    Also Updates the related Delivery Notes.
    """
    from erpnext_shipping.erpnext_shipping.shipping import update_tracking

    shipments = frappe.get_all(
        "Shipment",
        filters={
            "docstatus": 1,
            "status": "Booked",
            "shipment_id": ["!=", ""],
            "tracking_status": ["!=", "Delivered"],
        },
    )
    for shipment in shipments:
        shipment_doc = frappe.get_doc("Shipment", shipment.name)
        # Extract list of delivery_note values from the child table
        delivery_notes_list = [row.delivery_note for row in shipment_doc.shipment_delivery_note or [] if row.delivery_note]
        tracking_info = update_tracking(
            shipment.name,
            shipment_doc.service_provider,
            shipment_doc.shipment_id,
            delivery_notes_list,
        )


