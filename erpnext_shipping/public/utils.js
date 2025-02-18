export function verify_address(frm, success_function, fail_function, corrected_action) {
	frappe.call({
		method: "frappe.client.get",
		args: {
			doctype: "Address",
			name: frm.doc.delivery_address_name
		},
		callback: function(r) {
			let address = r.message

			frappe.call({
				method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.verify_address",
					freeze: true,
					args: {
						address_name: frm.doc.delivery_address_name,
						address_line1: address.address_line1,
						address_line2: address.address_line2,
						city: address.city,
						state: address.state,
						pincode: address.pincode,
						country: address.country
					},
					freeze_message: "Verifying Address",
					callback: function(r) {
						if(r.message && r.message == "success") {
							success_function()
						}
						else if(r.message && r.message == "fail") {
							fail_function()
						}
						else {
							const dialog = new frappe.ui.Dialog({
								title: __("Message"),
								size: "medium",
								fields: [
									{
										fieldtype: "HTML",
										fieldname: "message",
										options: r.message,
									},
								],
								primary_action_label: corrected_action.primary_label,
								primary_action: () => {
									corrected_action.primary_action()
								},
							});

							dialog.set_secondary_action(() => {
								corrected_action.secondary_action()
							});
							dialog.set_secondary_action_label(corrected_action.secondary_label)

							dialog.show()
						}
					}
			})
		}
	})
}