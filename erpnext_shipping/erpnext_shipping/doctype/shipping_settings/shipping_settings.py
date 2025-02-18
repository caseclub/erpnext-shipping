# Copyright (c) 2025, Frappe and contributors
# For license information, please see license.txt

import frappe
import requests
import json
from frappe.model.document import Document
from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

from erpnext_shipping.erpnext_shipping.utils import show_error_alert

class ShippingSettings(Document):
	def validate(self):
		if self.add_shipping_amount and not self.shipment_cost_target:
			frappe.throw("Please indicate how the shipping cost will be charged by selecting from the dropdown.")
		else:
			if self.shipment_cost_target == "Items List" and not self.item_code:
				frappe.throw("Please set the item code.")
			elif self.shipment_cost_target == "Taxes and Charges List":
				if not self.shipping_account and not self.shipping_description:
					frappe.throw("Please set the account head and the description.")
				elif not self.shipping_account:
					frappe.throw("Please set the account head.")
				elif not self.shipping_description:
					frappe.throw("Please set the description.")
	pass

@frappe.whitelist()
def check_settings_if_complete():
	shipping_settings = frappe.get_doc("Shipping Settings")
	shipment_cost_target = shipping_settings.shipment_cost_target
	form_link = '/app/shipping-settings?focus='
	# /app/shipping-settings/Shipping%20Settings?focus=

	def display_throw(error_message, field_name, resolution):
		frappe.throw(
			error_message +
			' Click <a href=/app/shipping-settings?focus=' +
			field_name +
			' onclick="window.location.replace(/app/shipping-settings?focus=' +
			field_name +
			')">here</a> ' +
			resolution + '.'
		)
		

	if shipping_settings.add_shipping_amount:
		if shipment_cost_target:
			if shipment_cost_target == "Items List":
				if not shipping_settings.item_code:
					display_throw(
						'The <b>item code</b> for Shipping and Handling has not been set.',
						'item_code',
						'to add the item code'
					)
				else:
					return "Complete"
			if shipment_cost_target == "Taxes and Charges List":
				if not (shipping_settings.shipping_description and shipping_settings.shipping_account):
					# dynamically change the focus parameter
					focus_target = 'shipping_description' if not shipping_settings.shipping_description and shipping_settings.shipping_account else 'shipping_account'
				
					display_throw(
						'The <b>account head and/or description</b> for the Shipping Charges has not been set.',
						focus_target,
						'to add them'
					)
				else:
					return "Complete"
		else:
			display_throw(
				'The <b>location</b> for Sales Invoice Shipping Cost has not been set.',
				shipment_cost_target,
				'to change the location'
			)

	else:
		display_throw(
			'Please turn on <b>automatic shipping amount charging</b> in the Shipping Settings to use this feature.',
			'add_shipping_amount',
			'to enable the feature'
		)
	
@frappe.whitelist() 
def make_sales_invoice_from_shipment(shipment):
	delivery_note = frappe.flags.args.delivery_note
	shipping_total = frappe.flags.args.shipping_total

	shipping_settings = frappe.get_doc("Shipping Settings")
	shipment_cost_target = shipping_settings.shipment_cost_target

	si_doc = make_sales_invoice(delivery_note)
	form_link = frappe.utils.get_url_to_form("Shipping Settings", "")

	if shipment_cost_target == "Items List":
		item_code = shipping_settings.item_code
		if item_code:
			si_doc.append("items", {
				'item_code': item_code,
				'description': frappe.db.get_value("Item", item_code, "description"),
				'qty': 1,
				'uom': frappe.db.get_value("Item", item_code, "stock_uom"),
				'rate': float(shipping_total),
				'price_list_rate': float(shipping_total)
			})
		else:
			frappe.throw('The item code for Shipping and Handling has not been set. Click <a href="{form_link}">here</a> to add the item code.'.format(form_link=form_link))

	if shipment_cost_target == "Taxes and Charges List":
		si_doc_dict = si_doc.as_dict() # make the si_doc a dict
		si_doc_shipping_entry_index = -1 # set the index of the shipping tax entry to null

		# iterate throught the taxes of the si_doc
		for index, tax_entry in enumerate(si_doc_dict.taxes): 
			if tax_entry['account_head'] == shipping_settings.shipping_account:
				# if a tax matches with the account head set in settings, return the index
				si_doc_shipping_entry_index = index
				break

		# check if settings are set
		if shipping_settings.shipping_description and shipping_settings.shipping_account:
			# check if shipping tax exists in the si doc (value 0 or more)
			if si_doc_shipping_entry_index >= 0:
				#if so modify the tax amount
				si_doc.taxes[si_doc_shipping_entry_index].tax_amount = float(shipping_total)
			# if not, manually add 
			else:
				si_doc.append("taxes", {
					'charge_type': "Actual",
					'description': shipping_settings.shipping_description,
					'account_head': shipping_settings.shipping_account,
					'rate': 0.00,
					'tax_amount': float(shipping_total)
				})
		else:
			frappe.throw('The account head and/or description for the Shipping Charges has not been set. Click <a href="{form_link}">here</a> to add them.'.format(form_link=form_link))

	si_doc.shipment = shipment

	return si_doc



@frappe.whitelist()
def verify_address(
	address_name
):
	address_doc = frappe.get_doc("Address", address_name)
	shipping_settings = frappe.get_single("Shipping Settings")
	verification_tool = shipping_settings.address_verification_tool

	address_line1 = address_doc.address_line1
	address_line2 = address_doc.address_line2 if address_doc.address_line2 else ""
	city = address_doc.city
	state = address_doc.state if address_doc.state else ""
	pincode = address_doc.pincode if address_doc.pincode else ""
	country = address_doc.country if address_doc.country else ""

	if verification_tool == "EasyPost":
		easypost_settings = frappe.get_single("EasyPost")
		# toggle between the test and production keys
		# self.selected_env = settings.test_key if settings.use_test_environment else settings.production_key
		# self.api_key = settings.get_password(self.selected_env)
		api_key = easypost_settings.get_password("test_key")

		# get the address values
		to_address= {
			'street1': address_line1,
			'street2': address_line2,
			'city': city,
			'state': state,
			'zip': pincode,
			'country': country
		}

		# create an address object and then verify
		try:
			response = requests.post(
				"https://api.easypost.com/v2/addresses/create_and_verify",
				json={
				"address": to_address,
				},
				auth=(api_key, "")
			)
			response_dict = response.json()
			
			verification_status = ""
			verification_notes = ""
			verification_data = {}

			if "address" in response_dict:
				response_address = response_dict["address"]
				highlight = ' style="background-color: #FFFF00"'

				def get_country_name(country):
					country_formatted = country.lower().strip()
					if len(country) <= 3:
						return frappe.db.get_value("Country", {"code": country_formatted}, "country_name").lower()
					else:
						return country_formatted

				def check_if_mismatch(is_same, string, field_name = None):
					string = string.strip()
					if string:
						if is_same:
							return f'<span>{string}</span><br/>'
						else:
							if field_name:
								verification_data[field_name] = string.title()
							return f'<mark>{string}</mark><br/>'
					else:
						return ''

				line1_diff = address_line1.lower().strip() == response_address['street1'].lower().strip()
				line2_diff = address_line2.lower().strip() == response_address['street2'].lower().strip()
				city_diff = city.lower().strip() == response_address['city'].lower().strip()
				state_diff = state.lower().strip() == response_address['state'].lower().strip()
				zip_diff = pincode.lower().strip() == response_address['zip'].lower().strip()
				country_diff = get_country_name(country) == get_country_name(response_address['country'])

				if (line1_diff and line2_diff and city_diff and state_diff and zip_diff and country_diff):
					verification_status = "Success"
				else:
					verification_status = "Mismatched"
					verification_notes = f'''
						<p>After verifying the address, we found some discrepancies between the address you entered and the address we found. Please see highlighted texts below:</p>
						<br/>
						<div class="row justify-content-around">
							<div class="py-3 px-4 border rounded border-1">
								<b>Address you entered:</b>
								<br/><br/>
								{check_if_mismatch(line1_diff, address_line1)}
								{check_if_mismatch(line2_diff, address_line2)}
								{check_if_mismatch(city_diff, city)}
								{check_if_mismatch(state_diff, state)}
								{check_if_mismatch(zip_diff, pincode)}
								{check_if_mismatch(country_diff, country)}
							</div>
							<div class="py-3 px-4 border rounded border-1">
								<b>Address we found:</b>
								<br/><br/>
								{check_if_mismatch(line1_diff, response_address['street1'], 'address_line1')}
								{check_if_mismatch(line2_diff, response_address['street2'], 'address_line2')}
								{check_if_mismatch(city_diff, response_address['city'], 'city')}
								{check_if_mismatch(state_diff, response_address['state'], 'state')}
								{check_if_mismatch(zip_diff, response_address['zip'], 'pincode')}
								{check_if_mismatch(country_diff, response_address['country'], 'country')}
							</div>
						</div>
						'''
			else:
				if "error" in response_dict:
					verification_status = "Fail"

			address_doc.verification_status = verification_status
			if verification_status == "Success":
				address_doc.is_verified = 1
			else:
				address_doc.is_verified = 0
				
			address_doc.save()

			return {
				"result": verification_status,
				"notes": verification_notes,
				"data" : verification_data
			}
		except Exception:
			show_error_alert("verifying EasyPost Address")

@frappe.whitelist()
def update_address(
	address_name,
	data_map,
	do_verify_address=False
):
	data = json.loads(data_map)

	address_doc = frappe.get_doc("Address", address_name)
	address_doc.update(data)
	address_doc.save()
	
	if do_verify_address == "true":
		verify_address(address_name)

@frappe.whitelist()
def validate_submission(shipment_name, address_name):
	is_currency_set = frappe.db.get_single_value("Shipping Settings", "rates_currency")
	is_single_parcel = len(frappe.get_doc("Shipment", shipment_name).as_dict().shipment_parcel) == 1
	is_address_verified = frappe.db.get_value("Address", address_name, "is_verified")
	error_list = []
	error_messages = {}

	shipping_settings_link = frappe.utils.get_url_to_form("Shipping Settings", "")
	address_link = frappe.utils.get_url_to_form("Address", address_name)

	if not is_currency_set:
		error_list.append("currency_not_set")
		error_messages["currency_not_set"] = 'The currency for rates has not been set. To prevent this error from happening, set Rates Currency <a target="_blank" href={}?focus=rates_currency>here</a>.'.format(shipping_settings_link)
	if not is_single_parcel:
		error_list.append("multiple_parcels")
		error_messages["multiple_parcels"] = 'EasyPost will not appear in the rates table because there are multiple parcels in this shipment. To see EasyPost in the list, reduce your parcel to 1 only.'

	if not is_address_verified:
		error_list.append("unverified_address")
		error_messages["unverified_address"] = 'The address is unverified so ensure that it\'s correct. To correct the address or perform a verification, visit the address doc <a target="blank" href={}>here</a>'.format(address_link)

	def formulate_digest_message():
		message = ""

		for key in error_messages:
			message += "<li>" + error_messages[key] + "</li>"

		return "The submission was halted because of the following:<br/><ol>{}</ol>".format(message)

	return {
		"status": "validated" if is_currency_set and is_single_parcel and is_address_verified else "unvalidated",
		"error_type": "digest" if not is_currency_set and len(error_list) > 1 else "individual",
		"error_list": error_list,
		"error_messages": formulate_digest_message() if not is_currency_set and len(error_list) > 1 else error_messages
	}


