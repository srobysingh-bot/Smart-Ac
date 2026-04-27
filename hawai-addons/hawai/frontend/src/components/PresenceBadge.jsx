import { User, UserX } from 'lucide-react'

export default function PresenceBadge({ present }) {
  return present ? (
    <span className="flex items-center gap-1.5 chip bg-blue-900/40 text-blue-300">
      <User size={13} />
      Occupied
    </span>
  ) : (
    <span className="flex items-center gap-1.5 chip bg-gray-800 text-gray-500">
      <UserX size={13} />
      Vacant
    </span>
  )
}
