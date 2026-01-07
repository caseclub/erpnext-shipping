// Copyright (c) 2020, Frappe and contributors
// For license information, please see license.txt
///apps/erpnext_shipping/erpnext_shipping/public/js

/* ── Targeted debug helpers ─────────────────────────────────────── */
const DEBUG_PREFIX = "[ZBP-PRINT][Shipment]";
function log(...args) { console.log(DEBUG_PREFIX, ...args); } // only for problem-area logs
function warn(...args) { console.warn(DEBUG_PREFIX, ...args); }
function err(...args) { console.error(DEBUG_PREFIX, ...args); }
/* ── Utility: timeout wrapper ───────────────────────────────────── */
function withTimeout(promise, ms, label) {
    let t;
    const timeout = new Promise((_, reject) => {
        t = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(t));
}
/* ── Load BrowserPrint.js from ERPNext assets (local, no CDN) ───── */
async function ensureBrowserPrintLoaded() {
    log("ensureBrowserPrintLoaded: START");
    if (window.BrowserPrint) {
        log("ensureBrowserPrintLoaded: BrowserPrint already loaded");
        return;
    }
    const baseUrl = "/assets/workflow_automation/js/BrowserPrint.js";
    const url = `${baseUrl}?v=${Date.now()}`; // cache-bust
    await new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = url;
        s.async = true;
        s.onload = () => {
            if (window.BrowserPrint) {
                log("ensureBrowserPrintLoaded: loaded OK", baseUrl);
                resolve();
            } else {
                reject(new Error("BrowserPrint.js loaded but window.BrowserPrint is still undefined"));
            }
        };
        s.onerror = (e) => {
            err("ensureBrowserPrintLoaded: FAILED to load", baseUrl, e);
            reject(e);
        };
        document.head.appendChild(s);
        log("ensureBrowserPrintLoaded: injected", baseUrl);
    });
    log("ensureBrowserPrintLoaded: END");
}

/* ── Fetch printer IP from EasyPost (cache in memory) ───────────── */
let __cc_easypost_printer_ip_cache;

async function getEasyPostPrinterIp() {
    if (__cc_easypost_printer_ip_cache !== undefined) {
        return __cc_easypost_printer_ip_cache; // may be null/empty, intentionally cached
    }

    const r = await frappe.call({
        method: "frappe.client.get",
        args: { doctype: "EasyPost" } // assumes EasyPost is a Single DocType
    });

    const ip = (r?.message?.custom_printer_ip_address || "").trim();

    // Cache even if blank, to avoid repeated server calls
    __cc_easypost_printer_ip_cache = ip || null;

    log("EasyPost printer IP fetched", { ip: __cc_easypost_printer_ip_cache });

    return __cc_easypost_printer_ip_cache;
}


/* ── Discover and select printer (prefers EasyPost IP) ──────────── */
async function getZebraPrinter(targetIp) {
    log("getZebraPrinter: START", { targetIp });
    await ensureBrowserPrintLoaded();

    if (!window.BrowserPrint) {
        throw new Error("BrowserPrint library not available.");
    }

    return withTimeout(new Promise((resolve, reject) => {
        BrowserPrint.getLocalDevices((devices) => {
            log("getZebraPrinter: Devices discovered", devices);

            if (!devices || !devices.length) {
                reject(new Error("No printers discovered via Browser Print."));
                return;
            }

            const isZebra = (d) => (d.manufacturer || "").toLowerCase().includes("zebra");
            const hasZT411Name = (d) => (d.name || "").toLowerCase().includes("zt411");

            let selected = null;

            // 1) Best match: Zebra + ZT411 + UID contains targetIp
            if (targetIp) {
                selected = devices.find(d =>
                    isZebra(d) &&
                    hasZT411Name(d) &&
                    d.uid &&
                    d.uid.includes(targetIp)
                );
            }

            // 2) Next: Zebra + ZT411
            if (!selected) {
                selected = devices.find(d => isZebra(d) && hasZT411Name(d));
            }

            // 3) Next: any Zebra
            if (!selected) {
                selected = devices.find(d => isZebra(d));
            }

            if (!selected) {
                reject(new Error("No matching Zebra printer found (ZT411 preferred)."));
                return;
            }

            log("PRINTER SELECTED:", selected.name, selected.uid);
            resolve(selected);
        }, reject, "printer");
    }), 10000, "BrowserPrint.getLocalDevices()");
}

/* ── Print (logs around discovery and send) ─────────────────────── */
async function printZPLToPrinter(zpl) {
    log("printZPLToPrinter: START", { zplLength: zpl?.length || 0 });

    const targetIp = await getEasyPostPrinterIp();

    // If IP is missing, we still proceed (it will fall back to ZT411/Zebra selection)
    if (!targetIp) {
        warn("EasyPost custom_printer_ip_address is empty; falling back to ZT411/Zebra selection.");
    }

    const printer = await getZebraPrinter(targetIp);
    log("printZPLToPrinter: Printer ready");

    return withTimeout(new Promise((resolve, reject) => {
        printer.send(zpl, () => {
            log("printZPLToPrinter: Print succeeded");
            resolve();
        }, (e) => {
            err("printZPLToPrinter: Print failed", e);
            reject(e);
        });
    }), 15000, "printer.send()");
}


async function printThermalLabel(frm) {
    log("printThermalLabel: START");
    try {
        frappe.show_alert({ message: "Fetching ZPL…", indicator: "blue" });
        const r = await frappe.call({
            method: "erpnext_shipping.erpnext_shipping.shipping.get_shipment_zpl",
            args: { shipment: frm.doc.name }
        });
        log("Frappe call for ZPL completed", { hasZpl: !!r.message?.zpl });
        let zpl = r.message?.zpl;
        if (!zpl) {
            frappe.msgprint("No ZPL returned for this Shipment.");
            return;
        }
        if (Array.isArray(zpl)) zpl = zpl.join("\n\n");  // Concat multi-labels
        frappe.show_alert({ message: "Sending to printer…", indicator: "blue" });
        await printZPLToPrinter(zpl);
        frappe.show_alert({ message: "Label printed automatically.", indicator: "green" });
        log("printThermalLabel: END (success)");
    } catch (e) {
        err("printThermalLabel: FAILED", e);
        frappe.msgprint(`Auto-print failed: ${e.message || e}`);
    }
}

frappe.ui.form.on("Shipment", {
	refresh: function (frm) {
		if (frm.doc.docstatus === 1 && !frm.doc.shipment_id) {
			frm.add_custom_button(__("Fetch Shipping Rates"), function () {
				frm.events.fetch_shipping_rates(frm);
			});
		}

		if (frm.doc.shipment_id) {
			frm.add_custom_button(
				__("Print Shipping Label"),
				function () {
					return frm.events.print_shipping_label(frm);
				},
				__("Tools")
			);
		}

		if (frm.doc.shipment_id) {
			if (frm.doc.tracking_status != "Delivered") {
				frm.add_custom_button(
					__("Update Tracking"),
					function () {
						return frm.events.update_tracking(
							frm,
							frm.doc.service_provider,
							frm.doc.shipment_id
						);
					},
					__("Tools")
				);

				frm.add_custom_button(
					__("Track Status"),
					function () {
						if (frm.doc.tracking_url) {
							const urls = frm.doc.tracking_url.split(", ");
							urls.forEach((url) => window.open(url));
						} else {
							let msg = __(
								"Please complete Shipment (ID: {0}) on {1} and Update Tracking.",
								[frm.doc.shipment_id, frm.doc.service_provider]
							);
							frappe.msgprint({ message: msg, title: __("Incomplete Shipment") });
						}
					},
					__("View")
				);
			}
		}

		if (frm.doc.status === "Booked" || frm.doc.status === "Completed") {
    		frm.add_custom_button(
    		    "Create Sales Invoice", 
    		    function() {
					frappe.call({
						method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.check_settings_if_complete",
						freeze: true,
						freeze_message: "Checking Setttings",
						callback: function(r) {
							if (!r.exc) {
								if (frm.doc.shipment_delivery_note) {
									frappe.call({
										method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.find_related_shipments",
										args: {
											delivery_note_name: frm.doc.shipment_delivery_note[0].delivery_note,
											current_shipment: frm.doc.name
										},
										callback: function(r) {
											if (r.message) {
												let shipments = [
													{
														__checked: true,
														name: frm.doc.name,
														value_of_goods: frm.doc.value_of_goods,
														description_of_content: frm.doc.description_of_content, 
														shipment_amount: frm.doc.shipment_amount, 
														creation: frm.doc.creation, 
														shipment_type: frm.doc.shipment_type, 
														pickup_type: frm.doc.pickup_type
													},
													...r.message
												]
												let shipment_cost = frm.doc.shipment_amount
												let additional_fields = []
												let dialog_size = 'small'
												let shipment_cost_label = 'Shipment Cost'
												let shipment_list = []

												if (shipments.length > 1) {
													dialog_size = 'large'
													shipment_cost_label = 'Total Shipment Cost'
													additional_fields =  [
														{
															fieldtype: 'Section Break'
														},
														{
															label: 'Additional Message',
															fieldname: 'additional_message',
															fieldtype: 'HTML',
															options: __('There are other shipments similar to this. Would you like to include them to the invoice?<br/><br/>')
														},
														{
															label: 'Related Shipments Table',
															fieldname: 'related_shipments',
															fieldtype: 'Table',
															cannot_add_rows: 1,
															cannot_delete_rows: 1,
															cannot_edit_rows: 1,
															in_place_edit: 0,
															allow_bulk_edit: 0,
															data: shipments,
															fields: [
																// { 
																// 	fieldname: 'is_included',
																// 	fieldtype: 'Check',
																// 	in_list_view: 1,
																// 	label: 'Include?'
																// },
																{
																	fieldname: 'name',
																	fieldtype: 'Link',
																	in_list_view: true,
																	label: 'Shipment ID',
																	options: 'Shipment',
																	read_only:  1,
																	columns: 3
																},
																{ 
																	fieldname: 'value_of_goods',
																	fieldtype: 'Currency',
																	in_list_view: 1,
																	label: 'Value',
																	read_only:  1,
																	columns: 2
																},
																{ 
																	fieldname: 'description_of_content',
																	fieldtype: 'Data',
																	in_list_view: 1,
																	label: 'Description',
																	read_only:  1,
																	columns: 3
																},
																{ 
																	fieldname: 'shipment_amount',
																	fieldtype: 'Currency',
																	in_list_view: 1,
																	label: 'Shipment Amount',
																	read_only:  1,
																	columns: 2
																},
																{
																	fieldname: 'creation',
																	fieldtype: 'Datetime',
																	label: 'Date Created',
																	read_only: 1,
																},
																{
																	fieldname: 'shipment_type',
																	fieldtype: 'Data',
																	label: 'Shipment Type',
																	read_only: 1,
																},
																{
																	fieldname: 'pickup_type',
																	fieldtype: 'Data',
																	label: 'Pickup Type',
																	read_only: 1
																}
															]
														},
														{
															fieldtype: 'Column Break',
															fieldname: 'shipping_cost_column'  // Start first column
														},
													]
												}

												let shipping_cost_dialog = new frappe.ui.Dialog({
													title: __('Add Shipping Cost'),
													fields: [
														...additional_fields,
														{
															label: shipment_cost_label,
															fieldname: 'shipment_cost',
															fieldtype: 'Currency',
															read_only: 1,
															default: shipment_cost
														},
														{
															label: 'Handling Fee',
															fieldname: 'handling_fee',
															fieldtype: 'Currency',
															default: 2
														}
													],
													size: dialog_size,
													primary_action_label: 'Proceed',
													primary_action: function (values) {
														frappe.model.open_mapped_doc({
															method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.make_sales_invoice_from_shipment",
															frm: frm,
															args: {
																delivery_note: frm.doc.shipment_delivery_note[0].delivery_note,
																shipping_total: values.shipment_cost + (values.handling_fee || 0),
																shipments: shipment_list
															},
															freeze: true,
															freeze_message: "Creating New Sales Invoice",
														})
													}
												})

												if (shipments.length > 1) {
													shipping_cost_dialog.$wrapper.find('.modal-dialog').attr('id', 'shipping-cost-modal')
													shipping_cost_dialog.$wrapper.find('.form-column[data-fieldname="__column_1"]').addClass('col-md-9')
													shipping_cost_dialog.$wrapper.find('.form-column[data-fieldname="shipping_cost_column"]').addClass('col-md-3')
													shipping_cost_dialog.$wrapper.find('.panel-title').hide()
													console.log(shipping_cost_dialog.$wrapper)
													shipping_cost_dialog.$wrapper.find('use[href="#icon-down"]').attr('href', '#icon-up')
													shipping_cost_dialog.$wrapper.find('use[href="#icon-edit"]').attr('href', '#icon-down')
													shipping_cost_dialog.$wrapper.find('div[data-fieldname="related_shipments"] .grid-row input[type="checkbox"]').on('change', function () {
														let table_data = shipping_cost_dialog.get_value('related_shipments')
														let selected_shipments = table_data.filter(row => row.__checked)
														let shipment_sum = selected_shipments.reduce((sum, shipment) => sum + shipment.shipment_amount, 0)
														shipment_list = selected_shipments.map(shipment => shipment.name)
														console.log(shipment_list)
														shipping_cost_dialog.set_value('shipment_cost', shipment_sum)
													})
												}
												
												shipping_cost_dialog.show()
											}
										}
									})
								}
								else {
									frappe.msgprint({
										title: "Can't Create Sales Invoice",
										indicator: "orange",
										message: "The shipment doesn't have a delivery note associated with it."
									});
								}
							}
						}
					})
    		    }
    		)
	    }
	},

	fetch_shipping_rates: function (frm) {
		if (!frm.doc.shipment_id) {
			frappe.call({
				method: "erpnext_shipping.erpnext_shipping.shipping.fetch_shipping_rates",
				freeze: true,
				freeze_message: __("Fetching Shipping Rates"),
				args: {
					pickup_from_type: frm.doc.pickup_from_type,
					delivery_to_type: frm.doc.delivery_to_type,
					pickup_address_name: frm.doc.pickup_address_name,
					delivery_address_name: frm.doc.delivery_address_name,
					parcels: frm.doc.shipment_parcel,
					description_of_content: frm.doc.description_of_content,
					pickup_date: frm.doc.pickup_date,
					pickup_contact_name:
						frm.doc.pickup_from_type === "Company"
							? frm.doc.pickup_contact_person
							: frm.doc.pickup_contact_name,
					delivery_contact_name: frm.doc.delivery_contact_name,
					value_of_goods: frm.doc.value_of_goods,
				},
				callback: function (r) {
					if (r.message && r.message.length) {
						select_from_available_services(frm, r.message);
					} else {
						frappe.msgprint({
							message: __("No Shipment Services available"),
							title: __("Note"),
						});
					}
				},
			});
		} else {
			frappe.throw(__("Shipment already created"));
		}
	},

	print_shipping_label: function (frm) {
		printThermalLabel(frm);
	},

	update_tracking: function (frm, service_provider, shipment_id) {
		let delivery_notes = [];
		(frm.doc.shipment_delivery_note || []).forEach((d) => {
			delivery_notes.push(d.delivery_note);
		});
		frappe.call({
			method: "erpnext_shipping.erpnext_shipping.shipping.update_tracking",
			freeze: true,
			freeze_message: __("Updating Tracking"),
			args: {
				shipment: frm.doc.name,
				shipment_id: shipment_id,
				service_provider: service_provider,
				delivery_notes: delivery_notes,
			},
			callback: function (r) {
				if (!r.exc) {
					frm.reload_doc();
					$('div[data-fieldname="shipment_information_section"]')[0].scrollIntoView();
				}
			},
		});
	},

	before_submit: async (frm) => {
		let shipping_settings = await get_shipping_settings()
		function show_rates_error(message) {
			frappe.throw(message)
		}

		async function show_parcel_count_warning() {
			frm.save('Submit');
		}

		async function verify_address() {
			let address_name = frm.doc.delivery_address_name

			const addition_dialog_fields = [
				{
					fieldtype: "HTML",
					fieldname: "message_line2",
					options: __("<div id='mark-message' class='alert alert-warning small'><div style='margin-bottom: 0.5rem'>If you believe the customer address is correct, tick the checkbox below to avoid this message in the future.</div></div>")
				},
				{
					fieldtype: "Check",
					fieldname: "mark_as_verified",
					label: "Mark as verified",
					default: 0
				}
			]

			function update_address(data_map, verify_address = false, freeze_message = null) {
				let freeze_options = 
					freeze_message? 
						{
							freeze: true,
							freeze_message: __(freeze_message),
						}
					:
						{}
				frappe.call({
					method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.update_address",
					args: {
						address_name: address_name,
						data_map: data_map,
						do_verify_address: verify_address
					},
					...freeze_options
				})
			}

			if (shipping_settings.verify_address) {
				frappe.call({
					method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.verify_address",
						freeze: true,
						args: {
							address_name: address_name
						},
						callback: function(r) {
							if(r.message && r.message.result === "Mismatched") {
								frappe.validated = false
								const mismatch_dialog = new frappe.ui.Dialog({
									title: __("Address Mismatch Found"),
									size: "medium",
									fields: [
										{
											fieldtype: "HTML",
											fieldname: "message",
											options: r.message.notes + "<br/><br/>",
										},
										...addition_dialog_fields
									],
									primary_action_label: __("Submit Anyway"),
									primary_action: (values) => {
										if (values.mark_as_verified) {
											update_address({"is_verified": 1}, false)
										}
										frm.save('Submit')
										mismatch_dialog.hide()
									},
								})
	
								mismatch_dialog.set_secondary_action_label(__("Fix Address"))
								mismatch_dialog.set_secondary_action(() => {
									update_address(r.message.data, true, "Fixing Address")
									frm.set_value('delivery_address_name', '')
									setTimeout(function() {
										frm.set_value('delivery_address_name', address_name)
										frm.save()
										frm.scroll_to_field('delivery_address')
									} , 1500)
									mismatch_dialog.hide()
								})
		
								mismatch_dialog.show()
								mismatch_dialog.$wrapper.find('div[data-fieldname="mark_as_verified"]').appendTo(mismatch_dialog.$wrapper.find('div[id="mark-message"]'))
							}
							else if(r.message && r.message.result === "Fail") {
								frappe.validated = false
								const fail_dialog = new frappe.ui.Dialog({
									title: __("Address Not Found"),
									size: "medium",
									fields: [
										{
											fieldtype: "HTML",
											fieldname: "message_line1",
											options: __("The address was not found at EasyPost. To ignore this warning, click Submit Anyway.<br/><br/>")
										},
										...addition_dialog_fields
									],
									primary_action_label: __("Submit Anyway"),
									primary_action: (values) => {
										if (values.mark_as_verified) {
											update_address({"is_verified": 1}, false)
										}
										frm.save('Submit')
										fail_dialog.hide()
									},
								})
								fail_dialog.show()

								fail_dialog.$wrapper.find('div[data-fieldname="mark_as_verified"]').appendTo(fail_dialog.$wrapper.find('div[id="mark-message"]'))
							}
						}
				})
			}
			else {
				let prompt = new Promise((resolve, reject) => {
					frappe.confirm(
						"The address isn't verified. Continue anyways?",
						() => resolve(),
						() => reject()
					);
				});
				await prompt.then(
					() => {
						prompt.hide()
						frm.save('Submit')
					},
					() => {
						prompt.hide()
						frappe.validated = false
						frappe.show_alert({
							message: "Shipment purchase was cancelled.",
							indicator: "red" 
						}, 7)
					}
				)
			}

		}
			
		frappe.call({
			method: "erpnext_shipping.erpnext_shipping.doctype.shipping_settings.shipping_settings.validate_submission",
			args: {
				shipment_name: frm.doc.name,
				address_name: frm.doc.delivery_address_name
			},
			freeze: true,
			freeze_message: __("Submitting Shipment"),
			callback: function(r) {
				let response = r.message
				if (response && response.status !== "validated") {
					frappe.validated = false
					if (response.error_type === "digest") {
						frappe.msgprint({
							title: __("Submission Halted"),
							indicator: "orange",
							message: __(response.error_messages),
							wide: true
						})
					}
					else {
						if (response.error_list.includes("currency_not_set")) {
							show_rates_error(response.error_messages.currency_not_set)
						}

						if (response.error_list.includes("multiple_parcels")) {
							show_parcel_count_warning()
						}

						if (response.error_list.includes("unverified_address")) {
							verify_address()
						}
					}
				}
			}
		})
	}
});

async function get_shipping_settings() {
	return response = await new Promise((resolve, reject) => {
		frappe.call({
			method: "frappe.client.get",
			args: {
				doctype: "Shipping Settings"
			},
			callback: function(r) {
				if (!r.exc) resolve(r.message);
                else reject(r.exc);
			}
		})
	})
}

async function net_print_shipping_label(shipment_name) {
	function print_label(selected_printer) {
		frappe.call({
			method: "erpnext_shipping.erpnext_shipping.shipping.net_print_shipping_label",
			args: {
				shipment: shipment_name,
				printer_setting: selected_printer,
			},
			callback: function (res) {
				if (!res.exc) {
					frappe.msgprint(
						__("Shipping label sent to printer successfully.")
					);
				}
			},
		});
	} 

	// Fetch Shipping settings to check for default_network_printer
	// Mark changed Easypost to Shipping Settings
	let shipping_settings = await get_shipping_settings()

	if (shipping_settings.default_network_printer) {
		// Default printer exists, skip the dialog
		print_label(shipping_settings.default_network_printer)
	}
}

async function select_from_available_services(frm, available_services) {
	const arranged_services = available_services.reduce(
		(prev, curr) => {
			if (curr.is_preferred) {
				prev.preferred_services.push(curr);
			} else {
				prev.other_services.push(curr);
			}
			return prev;
		},
		{ preferred_services: [], other_services: [] }
	);

	const select_dialog = new frappe.ui.Dialog({
		title: __("Select Service to Create Shipment"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "available_services",
				label: __("Available Services"),
			},
		],
	});

	let delivery_notes = [];
	(frm.doc.shipment_delivery_note || []).forEach((d) => {
		delivery_notes.push(d.delivery_note);
	});

	let shipping_settings = await get_shipping_settings()

	select_dialog.fields_dict.available_services.$wrapper.html(
	  frappe.render_template("shipment_service_selector", {
		header_columns: [__("Platform"), __("Carrier"), __("Parcel Service"), __("Days"), __("Price"), ""],
		data: arranged_services,
		rates_currency: shipping_settings.rates_currency
	  })
	);


	select_dialog.$body.on("click", ".btn", function () {
		let service_type = $(this).attr("data-type");
		let service_index = cint($(this).attr("id").split("-")[2]);
		let service_data = arranged_services[service_type][service_index];
		frm.select_row(service_data);
	});

	frm.select_row = function (service_data) {

		frappe.call({
			method: "erpnext_shipping.erpnext_shipping.shipping.create_shipment",
			freeze: true,
			freeze_message: __("Creating Shipment"),
			args: {
				shipment: frm.doc.name,
				pickup_from_type: frm.doc.pickup_from_type,
				delivery_to_type: frm.doc.delivery_to_type,
				pickup_address_name: frm.doc.pickup_address_name,
				delivery_address_name: frm.doc.delivery_address_name,
				shipment_parcel: frm.doc.shipment_parcel,
				description_of_content: frm.doc.description_of_content,
				pickup_date: frm.doc.pickup_date,
				pickup_contact_name:
					frm.doc.pickup_from_type === "Company"
						? frm.doc.pickup_contact_person
						: frm.doc.pickup_contact_name,
				delivery_contact_name: frm.doc.delivery_contact_name,
				value_of_goods: frm.doc.value_of_goods,
				service_data: service_data,
				delivery_notes: delivery_notes,
			},
			callback: function (r) {
				if (!r.exc) {
					frm.reload_doc();
					frappe.msgprint({
						message: __("Shipment {1} has been created with {0}.", [
							r.message.service_provider,
							r.message.shipment_id.bold(),
						]),
						title: __("Shipment Created"),
						indicator: "green",
					});
					
					// Automatically print only if Shipment.custom_auto_print_label is checked
					const should_auto_print = !!frm.doc.custom_auto_print_label;

					if (should_auto_print) {
						setTimeout(async () => {
							await printThermalLabel(frm);
						}, 500); // keep existing delay
					} else {
						log("Auto-print skipped (custom_auto_print_label is unchecked).");
					}
				
				
					frm.events.update_tracking(
						frm,
						r.message.service_provider,
						r.message.shipment_id
					);
					net_print_shipping_label(frm.doc.name);
				}
			},
		});
		select_dialog.hide();

	};
	select_dialog.show();
}