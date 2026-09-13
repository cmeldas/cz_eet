import OrderPaymentValidation from "@point_of_sale/app/utils/order_payment_validation";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

/**
 * EET: hned po uložení zaplacené objednávky na server požádá o odeslání tržby
 * správci daně a POK si uloží do objednávky, aby byl na účtence.
 *
 * Odeslání běží v samostatném požadavku až po uložení objednávky, takže tržba
 * nikdy neodejde bez objednávky v databázi. Když server neodpoví (pokladna
 * offline, výpadek EET), účtenka se vytiskne s poznámkou o offline režimu a
 * tržbu odešle cron.
 */
patch(OrderPaymentValidation.prototype, {
    async beforePostPushOrderResolve(order, order_server_ids) {
        const result = await super.beforePostPushOrderResolve(...arguments);
        if (this.pos.config.eet_enabled && order_server_ids?.length) {
            await this.eetSendNow(order, order_server_ids);
        }
        return result;
    },

    async eetSendNow(order, orderServerIds) {
        let data;
        try {
            const response = await this.pos.data.call("pos.order", "eet_send_now", [orderServerIds]);
            data = response?.[order.id];
        } catch (error) {
            console.warn("EET: odeslání tržby se nezdařilo", error);
        }
        if (data) {
            order.eet_state = data.eet_state || false;
            order.eet_pok = data.eet_pok || false;
        }
        if (!data?.eet_pok && data?.eet_state !== "verified") {
            this.pos.notification.add(
                _t("EET: tržba zatím nebyla zaevidována (offline režim), odešle se automaticky."),
                { type: "warning" }
            );
        }
    },
});
