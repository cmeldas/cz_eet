import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

/**
 * U pokladny s evidencí tržeb musí pokladna po uložení objednávky počkat na
 * odeslání tržby (beforePostPushOrderResolve), aby se POK dostal na účtenku.
 */
patch(PosOrder.prototype, {
    waitForPushOrder() {
        return this.config?.eet_enabled ? true : super.waitForPushOrder(...arguments);
    },
});
