import { createHmac, timingSafeEqual } from 'crypto';
import {
	NodeConnectionTypes,
	type IDataObject,
	type IHookFunctions,
	type INodeType,
	type INodeTypeDescription,
	type IWebhookFunctions,
	type IWebhookResponseData,
} from 'n8n-workflow';

async function api(this: IHookFunctions, method: 'POST' | 'DELETE', path: string, body?: IDataObject) {
	const credentials = await this.getCredentials('outreachDeskApi');
	const baseUrl = (credentials.baseUrl as string).replace(/\/$/, '');
	return this.helpers.httpRequestWithAuthentication.call(this, 'outreachDeskApi', {
		method,
		url: `${baseUrl}${path}`,
		body,
		json: true,
	});
}

export class OutreachDeskTrigger implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'Outreach Desk Trigger',
		name: 'outreachDeskTrigger',
		icon: { light: 'file:../../icons/outreach-desk.svg', dark: 'file:../../icons/outreach-desk.dark.svg' },
		group: ['trigger'],
		version: 1,
		subtitle: '={{$parameter["events"].join(", ")}}',
		description: 'Starts the workflow when outreach-desk drafts, sends, classifies a reply or moves a contact',
		defaults: { name: 'Outreach Desk Trigger' },
		inputs: [],
		outputs: [NodeConnectionTypes.Main],
		credentials: [{ name: 'outreachDeskApi', required: true }],
		webhooks: [{ name: 'default', httpMethod: 'POST', responseMode: 'onReceived', path: 'webhook' }],
		properties: [
			{
				displayName: 'Events',
				name: 'events',
				type: 'multiOptions',
				required: true,
				default: ['reply.classified'],
				options: [
					{
						name: 'Contact Stage Changed',
						value: 'contact.stage_changed',
						description: 'A contact moved along the pipeline',
					},
					{
						name: 'Draft Ready',
						value: 'draft.ready',
						description: 'A new draft is waiting for review',
					},
					{
						name: 'Email Sent',
						value: 'email.sent',
						description: 'An approved email was sent',
					},
					{
						name: 'Reply Classified',
						value: 'reply.classified',
						description: 'A reply was labelled (interested, meeting request, unsubscribe…)',
					},
				],
			},
			{
				displayName: 'Only Labels',
				name: 'labels',
				type: 'multiOptions',
				default: [],
				description: 'For Reply Classified: only trigger on these labels. Empty means all.',
				options: [
					{ name: 'Bounce', value: 'bounce' },
					{ name: 'Interested', value: 'interested' },
					{ name: 'Meeting Request', value: 'meeting_request' },
					{ name: 'Not Interested', value: 'not_interested' },
					{ name: 'Not Now', value: 'not_now' },
					{ name: 'Out of Office', value: 'out_of_office' },
					{ name: 'Referral', value: 'referral' },
					{ name: 'Unsubscribe', value: 'unsubscribe' },
				],
			},
		],
	};

	webhookMethods = {
		default: {
			async checkExists(this: IHookFunctions): Promise<boolean> {
				return Boolean(this.getWorkflowStaticData('node').webhookId);
			},
			async create(this: IHookFunctions): Promise<boolean> {
				const hook = (await api.call(this, 'POST', '/api/webhooks', {
					url: this.getNodeWebhookUrl('default'),
					events: this.getNodeParameter('events') as string[],
				})) as IDataObject;
				const data = this.getWorkflowStaticData('node');
				data.webhookId = hook.id;
				data.secret = hook.secret;
				return true;
			},
			async delete(this: IHookFunctions): Promise<boolean> {
				const data = this.getWorkflowStaticData('node');
				if (data.webhookId) {
					try {
						await api.call(this, 'DELETE', `/api/webhooks/${data.webhookId}`);
					} catch (error) {
						// Usually it was already removed on the outreach-desk side; nothing else to clean up.
						this.logger.warn(`Could not delete outreach-desk webhook ${data.webhookId}: ${error.message}`);
					}
					delete data.webhookId;
					delete data.secret;
				}
				return true;
			},
		},
	};

	async webhook(this: IWebhookFunctions): Promise<IWebhookResponseData> {
		const req = this.getRequestObject();
		const secret = this.getWorkflowStaticData('node').secret as string | undefined;
		const given = Buffer.from(String(req.headers['x-outreach-signature'] ?? ''));
		const expected = Buffer.from(
			'sha256=' + createHmac('sha256', secret ?? '').update(req.rawBody ?? '').digest('hex'),
		);
		if (!secret || given.length !== expected.length || !timingSafeEqual(given, expected)) {
			this.getResponseObject().status(401).send('Invalid signature');
			return { noWebhookResponse: true };
		}

		const body = this.getBodyData() as IDataObject;
		const labels = this.getNodeParameter('labels', []) as string[];
		const label = (body.data as IDataObject | undefined)?.label as string | undefined;
		if (body.event === 'reply.classified' && labels.length && !labels.includes(label ?? '')) {
			return { webhookResponse: 'ignored' };
		}
		return { workflowData: [this.helpers.returnJsonArray(body)] };
	}
}
