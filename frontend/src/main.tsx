import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import 'antd/dist/reset.css'
import App from './App'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#0066cc',
          colorInfo: '#0066cc',
          colorSuccess: '#1f7551',
          colorWarning: '#935c10',
          colorError: '#c13c3c',
          colorText: '#1d1d1f',
          colorTextSecondary: '#6e6e73',
          colorBorder: '#d7d7dd',
          colorBgLayout: '#f5f5f7',
          colorBgContainer: '#ffffff',
          borderRadius: 11,
          borderRadiusLG: 20,
          fontSize: 15,
          controlHeight: 42,
          fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif',
        },
        components: {
          Button: { controlHeight: 42, fontWeight: 500, primaryShadow: 'none', defaultShadow: 'none' },
          Card: { bodyPadding: 24, headerFontSize: 18 },
          Input: { activeShadow: '0 0 0 3px rgba(0, 102, 204, 0.12)' },
          Select: { optionSelectedBg: '#ebebf0', optionSelectedColor: '#1d1d1f' },
          Collapse: { headerBg: '#f7f7f9', contentBg: '#ffffff' },
        },
      }}
    >
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConfigProvider>
  </StrictMode>,
)
