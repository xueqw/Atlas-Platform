import React from 'react';import{createRoot}from'react-dom/client';import App from'./App';import{GovernanceConsole}from'./GovernanceDrawer';import'./styles.css';import'./knowledge.css';import'./agent.css';
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/><GovernanceConsole/></React.StrictMode>)
