import '@fontsource-variable/instrument-sans'
import '@fontsource-variable/jetbrains-mono'
import '@fontsource-variable/newsreader'
import './styles.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App, SpotlightApp } from './App'

const isSpotlight = window.location.hash.startsWith('#/spotlight')

createRoot(document.getElementById('root')!).render(
  <StrictMode>{isSpotlight ? <SpotlightApp /> : <App />}</StrictMode>,
)
