// Copyright (c) 2020, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Address", {
    refresh: function (frm) {
        if (!frm.doc.is_verified && frm.doc.verification_status == "Success") {
            frm.set_intro(__("Address was successfully verified."))
        }

        if (!frm.doc.is_verified && frm.doc.verification_status == "Fail") {
            frm.set_intro(__("Address wasn't found at EasyPost.com, after verifying. To ignore this error, simply tick the 'Is Verified' checkbox."), "orange")
        }

        if (!frm.doc.is_verified && frm.doc.verification_status == "Mismatched") {
            frm.set_intro(__("Address doesn't match what's at EasyPost.com, after verification. To learn more, click the 'Re-verify Address' button."), "orange")
        }

        let verification_btn_label = frm.doc.verification_status == ""? "Verify Address" : "Re-verify Address"
        frm.add_custom_button(__(verification_btn_label), function () {
            frappe.call({
                method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.verify_address",
                freeze: true,
                args: {
                    address_name: frm.doc.name,
                    address_line1: frm.doc.address_line1,
                    address_line2: frm.doc.address_line2,
                    city: frm.doc.city,
                    state: frm.doc.state,
                    pincode: frm.doc.pincode,
                    country: frm.doc.country
                },
                freeze_message: "Verifying Address",
                callback: function(r) {
                    if(r.message && r.message.result === "Success") {
                        frappe.msgprint({
                            message: __("The address was found at EasyPost and is now marked as verified."),
                            title: __("Address Found"),
                        })
                    }

                    if(r.message && r.message.result === "Mismatched") {
                        const dialog = new frappe.ui.Dialog({
                            title: __("Address Mismatch Found"),
                            size: "medium",
                            fields: [
                                {
                                    fieldtype: "HTML",
                                    fieldname: "message",
                                    options: r.message.notes,
                                },
                            ],
                            primary_action_label: __("Fix Address"),
                            primary_action: () => {
                                Object.entries(r.message.data).forEach(([fieldname, value]) => {
                                    frm.set_value(fieldname, value)
                                    frm.save()

                                    setTimeout(() => {
                                        frm.scroll_to_field(fieldname)
                                    }, 1000);
                                })
                                dialog.hide()
                            },
                        })

                        dialog.set_secondary_action(() => {
                            frm.set_value("is_verified", 1)
                            frm.save()
                            dialog.hide()
                        })

                        dialog.set_secondary_action_label(__("Mark as Verified"))

                        dialog.show()
                    }

                    if(r.message && r.message.result === "Fail") {
                        const dialog = new frappe.ui.Dialog({
                            title: __("Address Not Found"),
                            size: "medium",
                            fields: [
                                {
                                    fieldtype: "HTML",
                                    fieldname: "message",
                                    options: __("The address was not found at EasyPost. <br/><br/> To change the address, click Cancel. To ignore this warning, click Mark as Verified.")
                                },
                            ],
                            primary_action_label: __("Cancel"),
                            primary_action: () => {
                                dialog.hide()
                            },
                        })

                        dialog.set_secondary_action(() => {
                            frm.set_value("is_verified", 1)
                            frm.save()
                            dialog.hide()
                        })

                        dialog.set_secondary_action_label(__("Mark as Verified"))

                        dialog.show()
                    }
                }
            })
        });
    }
})