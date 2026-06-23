export type Message={id:string;role:'user'|'assistant';content:string;sources:string;created_at:string}
export type Conversation={id:string;title:string;created_at:string;updated_at:string;messages?:Message[]}
