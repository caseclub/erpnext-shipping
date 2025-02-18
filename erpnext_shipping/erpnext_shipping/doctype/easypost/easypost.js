// Copyright (c) 2024, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on("EasyPost", {
	refresh(frm) {
        if (frm.doc.printer_type === "Label Printer") {
            frm.set_df_property("label_format", "options", ["png", "zpl", "epl2"])
        }

        if (frm.doc.printer_type === "Document Printer") {
            frm.set_df_property("label_format", "options", ["pdf"])
        }

        frm.refresh_field("label_format")
	},

    printer_type(frm) {
        if (frm.doc.printer_type === "Label Printer") {
            frm.set_df_property("label_format", "options", ["png", "zpl", "epl2"])
            frm.set_value("label_format", "png")
        }

        if (frm.doc.printer_type === "Document Printer") {
            frm.set_df_property("label_format", "options", ["pdf"])
            frm.set_value("label_format", "pdf")
        }

        frm.refresh_field("label_format")
    }
});
