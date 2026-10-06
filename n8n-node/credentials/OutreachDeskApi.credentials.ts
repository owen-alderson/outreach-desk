import type {
	IAuthenticateGeneric,
	ICredentialTestRequest,
	ICredentialType,
	INodeProperties,
} from 'n8n-workflow';

export class OutreachDeskApi implements ICredentialType {
	name = 'outreachDeskApi';

	displayName = 'Outreach Desk API';

	icon = { light: 'file:../icons/outreach-desk.svg', dark: 'file:../icons/outreach-desk.dark.svg' } as const;

	documentationUrl = 'https://github.com/owen-alderson/outreach-desk/tree/main/n8n-node#credentials';

	properties: INodeProperties[] = [
		{
			displayName: 'Base URL',
			name: 'baseUrl',
			type: 'string',
			default: 'http://localhost:8000',
			placeholder: 'http://localhost:8000',
			description: 'Where outreach-desk is running. From n8n in Docker Compose, use http://outreach-desk:8000.',
			required: true,
		},
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			description: 'Shown on the Settings page of outreach-desk',
			required: true,
		},
	];

	authenticate: IAuthenticateGeneric = {
		type: 'generic',
		properties: {
			headers: {
				Authorization: '=Bearer {{$credentials.apiKey}}',
			},
		},
	};

	test: ICredentialTestRequest = {
		request: {
			baseURL: '={{$credentials.baseUrl.replace(/\\/$/, "")}}',
			url: '/api/campaigns',
		},
	};
}
