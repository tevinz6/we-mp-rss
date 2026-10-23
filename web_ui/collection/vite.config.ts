import {defineConfig} from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({plugins:[vue()],build:{outDir:'../../static/collector',emptyOutDir:true},server:{proxy:{'/api':'http://127.0.0.1:8001','/rss':'http://127.0.0.1:8001'}}})
