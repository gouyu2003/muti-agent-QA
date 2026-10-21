import {
  ArrowDown, ArrowRight, ArrowUp, Bot, BotMessageSquare, Boxes, Check,
  CircleCheck, CirclePause, CirclePlay, CircleX, Clock3, Copy, Database,
  Download, FileSearch, GitBranch, History, LoaderCircle, Menu, RefreshCw,
  Search, ShieldCheck, Square, TextWrap, Trash2, Workflow,
} from 'lucide-react'
import type { LucideIcon, LucideProps } from 'lucide-react'

// Preserve the existing icon API while keeping business icons in one family.
function icon(Component: LucideIcon) {
  return function Icon({ spin, className = '', ...props }: LucideProps & { spin?: boolean }) {
    return <Component {...props} className={`anticon app-icon ${spin ? 'icon-spinning' : ''} ${className}`} aria-hidden="true" focusable="false" />
  }
}

export const ApartmentOutlined = icon(Boxes)
export const ArrowDownOutlined = icon(ArrowDown)
export const ArrowRightOutlined = icon(ArrowRight)
export const ArrowUpOutlined = icon(ArrowUp)
export const BranchesOutlined = icon(GitBranch)
export const CheckCircleFilled = icon(CircleCheck)
export const BotMessageSquareOutlined = icon(BotMessageSquare)
export const CheckOutlined = icon(Check)
export const ClockCircleOutlined = icon(Clock3)
export const CloseCircleFilled = icon(CircleX)
export const CopyOutlined = icon(Copy)
export const DatabaseOutlined = icon(Database)
export const DownloadOutlined = icon(Download)
export const FileSearchOutlined = icon(FileSearch)
export const HistoryOutlined = icon(History)
export const LoadingOutlined = icon(LoaderCircle)
export const MenuOutlined = icon(Menu)
export const MenuUnfoldOutlined = icon(TextWrap)
export const PauseCircleOutlined = icon(CirclePause)
export const PlayCircleFilled = icon(CirclePlay)
export const ReloadOutlined = icon(RefreshCw)
export const RobotOutlined = icon(Bot)
export const SafetyCertificateOutlined = icon(ShieldCheck)
export const SearchOutlined = icon(Search)
export const StopOutlined = icon(Square)
export const DeleteOutlined = icon(Trash2)
export const WorkflowOutlined = icon(Workflow)
