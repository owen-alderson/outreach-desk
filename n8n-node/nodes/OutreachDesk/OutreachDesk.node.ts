import { NodeConnectionTypes, type INodeProperties, type INodeType, type INodeTypeDescription } from 'n8n-workflow';

const show = (resource: string, operation?: string[]) => ({
	show: operation ? { resource: [resource], operation } : { resource: [resource] },
});

const contactFields: INodeProperties[] = [
	{
		displayName: 'Role',
		name: 'role',
		type: 'string',
		default: '',
		routing: { send: { type: 'body', property: 'role' } },
	},
	{
		displayName: 'Company',
		name: 'company',
		type: 'string',
		default: '',
		routing: { send: { type: 'body', property: 'company' } },
	},
	{
		displayName: 'Facts',
		name: 'facts',
		type: 'string',
		typeOptions: { rows: 4 },
		default: '',
		description: 'What you know about them. Drafts may only personalise from these facts.',
		routing: { send: { type: 'body', property: 'facts' } },
	},
];

const stageOptions = ['new', 'contacted', 'replied', 'meeting', 'won', 'lost'].map((s) => ({
	name: s.charAt(0).toUpperCase() + s.slice(1),
	value: s,
}));

const contact: INodeProperties[] = [
	{
		displayName: 'Operation',
		name: 'operation',
		type: 'options',
		noDataExpression: true,
		displayOptions: show('contact'),
		options: [
			{
				name: 'Create',
				value: 'create',
				action: 'Create a contact',
				routing: { request: { method: 'POST', url: '/api/contacts' } },
			},
			{
				name: 'Get Many',
				value: 'getAll',
				action: 'Get many contacts',
				routing: { request: { method: 'GET', url: '/api/contacts' } },
			},
			{
				name: 'Update',
				value: 'update',
				action: 'Update a contact',
				description: 'Change details or move them to a pipeline stage',
				routing: { request: { method: 'PATCH', url: '=/api/contacts/{{$parameter.contactId}}' } },
			},
		],
		default: 'create',
	},
	{
		displayName: 'Name',
		name: 'name',
		type: 'string',
		required: true,
		default: '',
		displayOptions: show('contact', ['create']),
		routing: { send: { type: 'body', property: 'name' } },
	},
	{
		displayName: 'Email',
		name: 'email',
		type: 'string',
		placeholder: 'name@email.com',
		required: true,
		default: '',
		displayOptions: show('contact', ['create']),
		routing: { send: { type: 'body', property: 'email' } },
	},
	{
		displayName: 'Additional Fields',
		name: 'additionalFields',
		type: 'collection',
		placeholder: 'Add Field',
		default: {},
		displayOptions: show('contact', ['create']),
		options: contactFields,
	},
	{
		displayName: 'Contact ID',
		name: 'contactId',
		type: 'number',
		required: true,
		default: 0,
		displayOptions: show('contact', ['update']),
	},
	{
		displayName: 'Update Fields',
		name: 'updateFields',
		type: 'collection',
		placeholder: 'Add Field',
		default: {},
		displayOptions: show('contact', ['update']),
		options: [
			...contactFields,
			{
				displayName: 'Stage',
				name: 'stage',
				type: 'options',
				options: stageOptions,
				default: 'new',
				routing: { send: { type: 'body', property: 'stage' } },
			},
		],
	},
	{
		displayName: 'Stage',
		name: 'stage',
		type: 'options',
		options: [{ name: 'Any', value: '' }, ...stageOptions],
		default: '',
		displayOptions: show('contact', ['getAll']),
		routing: { send: { type: 'query', property: 'stage', value: '={{$value || undefined}}' } },
	},
];

const campaign: INodeProperties[] = [
	{
		displayName: 'Operation',
		name: 'operation',
		type: 'options',
		noDataExpression: true,
		displayOptions: show('campaign'),
		options: [
			{
				name: 'Enroll Contacts',
				value: 'enroll',
				action: 'Enroll contacts in a campaign',
				description: 'Start the sequence for these contacts. Drafts appear in the review queue.',
				routing: { request: { method: 'POST', url: '=/api/campaigns/{{$parameter.campaignId}}/enroll' } },
			},
			{
				name: 'Get Many',
				value: 'getAll',
				action: 'Get many campaigns',
				routing: { request: { method: 'GET', url: '/api/campaigns' } },
			},
		],
		default: 'enroll',
	},
	{
		displayName: 'Campaign ID',
		name: 'campaignId',
		type: 'number',
		required: true,
		default: 0,
		displayOptions: show('campaign', ['enroll']),
	},
	{
		displayName: 'Emails',
		name: 'emails',
		type: 'string',
		default: '',
		placeholder: 'ada@example.com, grace@example.com',
		description: 'Comma-separated emails of existing contacts',
		displayOptions: show('campaign', ['enroll']),
		routing: {
			send: {
				type: 'body',
				property: 'emails',
				value: '={{$value.split(",").map((e) => e.trim()).filter((e) => e)}}',
			},
		},
	},
];

const draft: INodeProperties[] = [
	{
		displayName: 'Operation',
		name: 'operation',
		type: 'options',
		noDataExpression: true,
		displayOptions: show('draft'),
		options: [
			{
				name: 'Approve',
				value: 'approve',
				action: 'Approve a draft for sending',
				description: 'Queue the draft to send in the next send window, optionally with edits',
				routing: { request: { method: 'POST', url: '=/api/drafts/{{$parameter.draftId}}/approve' } },
			},
			{
				name: 'Get Many',
				value: 'getAll',
				action: 'Get drafts waiting for review',
				routing: { request: { method: 'GET', url: '/api/drafts' } },
			},
			{
				name: 'Reject',
				value: 'reject',
				action: 'Reject a draft',
				description: "Discard the draft and stop this contact's sequence",
				routing: { request: { method: 'POST', url: '=/api/drafts/{{$parameter.draftId}}/reject' } },
			},
		],
		default: 'getAll',
	},
	{
		displayName: 'Draft ID',
		name: 'draftId',
		type: 'number',
		required: true,
		default: 0,
		displayOptions: show('draft', ['approve', 'reject']),
	},
	{
		displayName: 'Edits',
		name: 'edits',
		type: 'collection',
		placeholder: 'Add Edit',
		default: {},
		displayOptions: show('draft', ['approve']),
		options: [
			{
				displayName: 'Subject',
				name: 'subject',
				type: 'string',
				default: '',
				routing: { send: { type: 'body', property: 'subject' } },
			},
			{
				displayName: 'Body',
				name: 'body',
				type: 'string',
				typeOptions: { rows: 6 },
				default: '',
				routing: { send: { type: 'body', property: 'body' } },
			},
		],
	},
];

export class OutreachDesk implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'Outreach Desk',
		name: 'outreachDesk',
		icon: { light: 'file:../../icons/outreach-desk.svg', dark: 'file:../../icons/outreach-desk.dark.svg' },
		group: ['output'],
		version: 1,
		subtitle: '={{$parameter["operation"] + ": " + $parameter["resource"]}}',
		description: 'Add contacts, enrol them in campaigns and review drafts in outreach-desk',
		defaults: { name: 'Outreach Desk' },
		usableAsTool: true,
		inputs: [NodeConnectionTypes.Main],
		outputs: [NodeConnectionTypes.Main],
		credentials: [{ name: 'outreachDeskApi', required: true }],
		requestDefaults: {
			baseURL: '={{$credentials.baseUrl.replace(/\\/$/, "")}}',
			headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
		},
		properties: [
			{
				displayName: 'Resource',
				name: 'resource',
				type: 'options',
				noDataExpression: true,
				options: [
					{ name: 'Campaign', value: 'campaign' },
					{ name: 'Contact', value: 'contact' },
					{ name: 'Draft', value: 'draft' },
				],
				default: 'contact',
			},
			...contact,
			...campaign,
			...draft,
		],
	};
}
