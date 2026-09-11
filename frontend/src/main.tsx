import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { AppProvider } from './core';
import App from './App';
import { TooltipProvider } from './components/Tooltip';
import './styles.css';
import './console.css';
import './login.css';
import './upload-templates.css';
import './scrollbars.css';
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppProvider>
        <TooltipProvider>
          <App />
        </TooltipProvider>
      </AppProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
