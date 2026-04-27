import { useState } from 'react'

/**
 * ACSelector — cascading Brand → Model dropdowns with search.
 *
 * Props:
 *   brands          Array from /api/brands
 *   selectedBrand   string brand id
 *   selectedModel   string model id
 *   onBrandChange   (brandId: string) => void
 *   onModelChange   (modelId: string) => void
 */
export default function ACSelector({
  brands = [],
  selectedBrand,
  selectedModel,
  onBrandChange,
  onModelChange,
}) {
  const [brandQuery, setBrandQuery] = useState('')
  const [modelQuery, setModelQuery] = useState('')

  const currentBrand = brands.find(b => b.id === selectedBrand)
  const models       = currentBrand?.models || []

  const filteredBrands = brands.filter(b =>
    b.name.toLowerCase().includes(brandQuery.toLowerCase())
  )
  const filteredModels = models.filter(m =>
    `${m.name} ${m.series}`.toLowerCase().includes(modelQuery.toLowerCase())
  )

  const selectClass =
    'w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 ' +
    'focus:outline-none focus:border-blue-500'
  const searchClass =
    'w-full mb-2 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 ' +
    'focus:outline-none focus:border-blue-500'

  return (
    <div className="grid grid-cols-2 gap-3">
      {/* Brand */}
      <div>
        <label className="text-sm text-gray-400 block mb-1">AC Brand</label>
        <input
          type="search"
          placeholder="Search brands…"
          value={brandQuery}
          onChange={e => setBrandQuery(e.target.value)}
          className={searchClass}
        />
        <select
          className={selectClass}
          value={selectedBrand || ''}
          onChange={e => {
            setBrandQuery('')
            onBrandChange(e.target.value)
          }}
        >
          <option value="">Select brand…</option>
          {filteredBrands.map(b => (
            <option key={b.id} value={b.id}>{b.name}</option>
          ))}
        </select>
      </div>

      {/* Model */}
      <div>
        <label className="text-sm text-gray-400 block mb-1">AC Model</label>
        <input
          type="search"
          placeholder="Search models…"
          value={modelQuery}
          onChange={e => setModelQuery(e.target.value)}
          className={searchClass}
          disabled={!selectedBrand}
        />
        <select
          className={selectClass}
          value={selectedModel || ''}
          onChange={e => {
            setModelQuery('')
            onModelChange(e.target.value)
          }}
          disabled={!selectedBrand}
        >
          <option value="">Select model…</option>
          {filteredModels.map(m => (
            <option key={m.id} value={m.id}>{m.name} — {m.series}</option>
          ))}
        </select>
      </div>

      {/* Model detail */}
      {selectedModel && currentBrand && (() => {
        const model = models.find(m => m.id === selectedModel)
        if (!model) return null
        return (
          <div className="col-span-2 bg-gray-800/50 rounded-lg px-3 py-2 text-xs text-gray-400 flex flex-wrap gap-3">
            <span>Modes: {model.supported_modes?.join(', ')}</span>
            <span>Temp: {model.temp_range?.[0]}–{model.temp_range?.[1]}°C</span>
            <span>Fan: {model.fan_speeds?.join(', ')}</span>
          </div>
        )
      })()}
    </div>
  )
}
