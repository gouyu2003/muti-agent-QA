import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
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
          colorPrimary: '#3157d5',
          colorInfo: '#3157d5',
          colorSuccess: '#138a8a',
          colorWarning: '#c77816',
          colorError: '#c44444',
          colorText: '#172033',
          colorTextSecondary: '#667389',
          colorBorder: '#d8e1ec',
          borderRadius: 10,
          fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif',
        },
        components: {
          Button: { controlHeight: 38, fontWeight: 600 },
          Card: { bodyPadding: 20 },
          Input: { activeShadow: '0 0 0 3px rgba(49, 87, 213, 0.1)' },
        },
      }}
    >
      <App />
    </ConfigProvider>
  </StrictMode>,
)
