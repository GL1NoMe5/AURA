import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AmbientProvider } from './AmbientSound';
import './styles.css';
import './interface.css';
import './demo/demo.css';
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><AmbientProvider><App /></AmbientProvider></React.StrictMode>);
