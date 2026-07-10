export const ROUTES = {
  LOGIN: '/login',
  GAMES: '/games',
  SYSTEM: (gameId: string) => `/games/${gameId}`,
  STORY: '/story',
  CHARACTER: '/character',
  LOBBY: '/lobby',
  ROOM: '/room',
} as const
