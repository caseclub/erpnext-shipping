// Copyright (c) 2025, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("Shipping Settings", {
	refresh(frm) {
        let fieldToFocus = new URLSearchParams(window.location.search).get('focus') // check if there's a field to autofocus on through URL parameter

        if (fieldToFocus) {
            frm.scroll_to_field(fieldToFocus)
        }
	},
    
});
